import re

import pgserver
import psycopg
import pytest

from app import create_app


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    yield server.get_uri()


def reset_db(pg_uri):
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


@pytest.fixture
def app(pg_uri):
    reset_db(pg_uri)
    return create_app({"DATABASE_URL": pg_uri, "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def ids(app, table, column):
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        return {name: id for id, name in conn.execute(f"SELECT id, {column} FROM {table}")}


def location_ids(app):
    return ids(app, "locations", "short_name")


def category_ids(app):
    return ids(app, "categories", "name")


def stock(app, recipe_id, category=None):
    """{location short name: quantity}, summed across categories unless one is given."""
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        rows = conn.execute(
            """SELECT l.short_name, sum(s.quantity)::int
               FROM stock s JOIN locations l ON l.id = s.location_id
               JOIN categories c ON c.id = s.category_id
               WHERE s.recipe_id = %s AND (%s::text IS NULL OR c.name = %s)
               GROUP BY l.short_name""",
            (recipe_id, category, category),
        )
        return {name: qty for name, qty in rows if qty}


def add_recipe(client, name="Chicken & Rice"):
    resp = client.post("/recipes/new", data={"name": name, "ingredients": "chicken\nrice"})
    assert resp.status_code == 302
    return int(re.search(r"/recipes/(\d+)", resp.headers["Location"]).group(1))


def make_batch(client, app, recipe_id, category="Lunch", made_on="2026-10-12", **split):
    locs = location_ids(app)
    data = {"recipe_id": recipe_id, "made_on": made_on}
    if category:
        data["category_id"] = category_ids(app)[category]
    data.update({f"qty_{locs[name]}": qty for name, qty in split.items()})
    return client.post("/make", data=data, follow_redirects=True)


def move(client, app, recipe_id, kind, quantity, frm, to=None, category="Lunch"):
    locs = location_ids(app)
    data = {"kind": kind, "meal": f"{recipe_id}:{category_ids(app)[category]}",
            "quantity": quantity, "from_location_id": locs[frm]}
    if to:
        data["to_location_id"] = locs[to]
    return client.post("/move", data=data, follow_redirects=True)


def test_healthz_is_public(app):
    assert app.test_client().get("/healthz").data == b"ok"


def test_make_batch_splits_across_locations(client, app):
    rid = add_recipe(client)
    resp = make_batch(client, app, rid, Freezer=3, Thawing=3)
    assert b"Added 6 meals of Chicken &amp; Rice (Lunch)" in resp.data
    assert stock(app, rid) == {"Freezer": 3, "Thawing": 3}
    # Second batch adds on top.
    make_batch(client, app, rid, Freezer=2)
    assert stock(app, rid) == {"Freezer": 5, "Thawing": 3}


def test_make_batch_needs_some_meals(client, app):
    rid = add_recipe(client)
    resp = make_batch(client, app, rid)
    assert b"at least one spot" in resp.data
    assert stock(app, rid) == {}


def test_make_batch_needs_a_category(client, app):
    rid = add_recipe(client)
    resp = make_batch(client, app, rid, category=None, Freezer=2)
    assert b"Pick breakfast, lunch, snack, or dinner" in resp.data
    assert stock(app, rid) == {}


def test_same_recipe_in_two_categories(client, app):
    rid = add_recipe(client, "Soup")
    make_batch(client, app, rid, category="Lunch", Freezer=3)
    make_batch(client, app, rid, category="Dinner", Freezer=2)
    assert stock(app, rid, "Lunch") == {"Freezer": 3}
    assert stock(app, rid, "Dinner") == {"Freezer": 2}

    # Using a dinner doesn't touch the lunches.
    move(client, app, rid, "used", 2, "Freezer", category="Dinner")
    assert stock(app, rid, "Lunch") == {"Freezer": 3}
    assert stock(app, rid, "Dinner") == {}
    resp = move(client, app, rid, "used", 1, "Freezer", category="Dinner")
    assert b"only 0 Soup (Dinner) in Freezer" in resp.data


def test_move_use_and_discard(client, app):
    rid = add_recipe(client)
    make_batch(client, app, rid, Freezer=4)

    move(client, app, rid, "moved", 2, "Freezer", "Thawing")
    assert stock(app, rid) == {"Freezer": 2, "Thawing": 2}

    move(client, app, rid, "moved", 1, "Thawing", "Syringes")
    resp = move(client, app, rid, "used", 1, "Syringes", to="Freezer")  # "to" ignored when used
    assert b"Used: 1 Chicken &amp; Rice (Lunch) from Fridge (in syringes)" in resp.data
    move(client, app, rid, "discarded", 1, "Freezer")
    assert stock(app, rid) == {"Freezer": 1, "Thawing": 1}


def test_cannot_move_more_than_available(client, app):
    rid = add_recipe(client)
    make_batch(client, app, rid, Freezer=1)
    resp = move(client, app, rid, "used", 2, "Freezer")
    assert b"only 1 Chicken &amp; Rice (Lunch) in Freezer" in resp.data
    resp = move(client, app, rid, "used", 1, "Thawing")
    assert b"only 0" in resp.data
    assert stock(app, rid) == {"Freezer": 1}


def test_bad_meal_choice(client, app):
    resp = client.post("/move", data={"kind": "used", "meal": "nonsense", "quantity": 1,
                                      "from_location_id": 1}, follow_redirects=True)
    assert b"Pick which meal" in resp.data


def test_inventory_grouped_by_category(client, app):
    stew = add_recipe(client, "Beef Stew")
    oats = add_recipe(client, "Oats")
    make_batch(client, app, stew, category="Dinner", Freezer=3, Syringes=2)
    make_batch(client, app, oats, category="Breakfast", Thawing=1)
    page = client.get("/").data.decode()

    cards = re.findall(r'stat-number">(\d+)</div>\s*<div class="stat-label">(\w+)', page)
    assert {name: int(n) for n, name in cards} == {"Breakfast": 1, "Lunch": 0, "Snack": 0, "Dinner": 5}
    assert "6 meals in all" in page

    dinner = page[page.index('id="cat-4"'):]
    assert "Beef Stew" in dinner and "Oats" not in dinner
    assert "category_id=4" in dinner  # number links go to the right category


def test_pages_render(client, app):
    rid = add_recipe(client, "Beef Stew")
    make_batch(client, app, rid, category="Snack", Freezer=3)
    history = client.get("/history").data.decode()
    assert 'Made</strong> 3 Beef Stew <span class="tag">Snack</span>' in history
    detail = client.get(f"/recipes/{rid}").data.decode()
    assert '<span class="tag">Snack</span>' in detail
    assert client.get(f"/recipes/{rid}/edit").status_code == 200
    page = client.get(f"/move?recipe_id={rid}&category_id=3&from_location_id=1").data.decode()
    assert f'value="{rid}:3" selected' in page
    assert client.get("/make").status_code == 200
    assert client.get("/recipes").status_code == 200


def test_undo(client, app):
    rid = add_recipe(client)
    make_batch(client, app, rid, category="Snack", Freezer=2)
    move(client, app, rid, "moved", 2, "Freezer", "Thawing", category="Snack")

    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        (made_id,), (move_id,) = conn.execute("SELECT id FROM movements ORDER BY id").fetchall()

    # Can't undo the batch while its meals have been moved out of the freezer.
    resp = client.post(f"/history/{made_id}/undo", follow_redirects=True)
    assert b"Can&#39;t undo" in resp.data
    client.post(f"/history/{move_id}/undo")
    assert stock(app, rid, "Snack") == {"Freezer": 2}
    client.post(f"/history/{made_id}/undo")
    assert stock(app, rid) == {}
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 0


def test_duplicate_recipe_name(client):
    add_recipe(client, "Soup")
    resp = client.post("/recipes/new", data={"name": "Soup"})
    assert resp.status_code == 200 and b"already a recipe" in resp.data


def test_archive_hides_from_batch_form(client, app):
    rid = add_recipe(client, "Old Recipe")
    client.post(f"/recipes/{rid}/edit", data={"name": "Old Recipe", "archived": "1"})
    assert b"Old Recipe" not in client.get("/make").data
    assert b"Old Recipe" in client.get("/recipes").data


def test_history_filter_by_recipe(client, app):
    soup = add_recipe(client, "Soup")
    stew = add_recipe(client, "Stew")
    make_batch(client, app, soup, Freezer=2)
    make_batch(client, app, stew, made_on="2026-09-01", Freezer=1)
    make_batch(client, app, stew, made_on="2026-09-20", Freezer=1)

    everything = client.get("/history").data.decode()
    assert "Soup" in everything and "Stew" in everything and "Last made" not in everything

    page = client.get(f"/history?recipe_id={stew}").data.decode()
    assert "Made</strong> 1 Stew" in page and "Made</strong> 2 Soup" not in page
    assert "Last made" in page and "Sep 20, 2026" in page

    never = add_recipe(client, "Never Made")
    assert b"hasn't been made yet" in client.get(f"/history?recipe_id={never}").data

    # Undo keeps the filter.
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        move_id = conn.execute("SELECT max(id) FROM movements").fetchone()[0]
    resp = client.post(f"/history/{move_id}/undo", data={"recipe_id": stew})
    assert resp.headers["Location"] == f"/history?recipe_id={stew}"

    # Junk filter values just show everything.
    assert client.get("/history?recipe_id=abc").status_code == 200
    assert client.get("/history?recipe_id=99999").status_code == 200


def test_old_recipe_columns_are_dropped(app, pg_uri):
    # A database created before instructions/notes were removed gets migrated on startup.
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("ALTER TABLE recipes ADD COLUMN instructions TEXT, ADD COLUMN notes TEXT")
    create_app({"DATABASE_URL": pg_uri})
    with psycopg.connect(pg_uri) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'recipes'")}
    assert "instructions" not in cols and "notes" not in cols and "ingredients" in cols


def test_pre_category_database_is_migrated_to_snack(pg_uri):
    # The shape of the live database before categories existed, with real-looking data.
    reset_db(pg_uri)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE locations (id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                                    short_name TEXT NOT NULL, sort_order INT NOT NULL DEFAULT 0);
            INSERT INTO locations (name, short_name, sort_order) VALUES
                ('Freezer', 'Freezer', 1), ('Fridge (thawing)', 'Thawing', 2),
                ('Fridge (in syringes)', 'Syringes', 3);
            CREATE TABLE recipes (id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                                  ingredients TEXT NOT NULL DEFAULT '', archived BOOLEAN NOT NULL DEFAULT FALSE,
                                  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE batches (id SERIAL PRIMARY KEY, recipe_id INT NOT NULL REFERENCES recipes(id),
                                  made_on DATE NOT NULL, notes TEXT NOT NULL DEFAULT '',
                                  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE stock (recipe_id INT NOT NULL REFERENCES recipes(id),
                                location_id INT NOT NULL REFERENCES locations(id),
                                quantity INT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                                PRIMARY KEY (recipe_id, location_id));
            CREATE TABLE movements (id SERIAL PRIMARY KEY, recipe_id INT NOT NULL REFERENCES recipes(id),
                                    batch_id INT REFERENCES batches(id), kind TEXT NOT NULL,
                                    from_location_id INT REFERENCES locations(id),
                                    to_location_id INT REFERENCES locations(id),
                                    quantity INT NOT NULL CHECK (quantity > 0),
                                    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            INSERT INTO recipes (name) VALUES ('Green Smoothie');
            INSERT INTO batches (recipe_id, made_on) VALUES (1, '2026-10-12');
            INSERT INTO stock VALUES (1, 1, 10), (1, 3, 1);
            INSERT INTO movements (recipe_id, batch_id, kind, to_location_id, quantity) VALUES (1, 1, 'made', 1, 10);
        """)

    app = create_app({"DATABASE_URL": pg_uri})
    create_app({"DATABASE_URL": pg_uri})  # running again is a no-op
    assert stock(app, 1, "Snack") == {"Freezer": 10, "Syringes": 1}
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT count(*) FROM batches WHERE category_id IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM movements WHERE category_id IS NULL").fetchone()[0] == 0

    # And the app works on the migrated data, including new categories alongside.
    client = app.test_client()
    make_batch(client, app, 1, category="Breakfast", Freezer=2)
    assert stock(app, 1, "Breakfast") == {"Freezer": 2}
    move(client, app, 1, "used", 1, "Syringes", category="Snack")
    assert stock(app, 1, "Snack") == {"Freezer": 10}


def test_edit_recipe_ingredients(client):
    rid = add_recipe(client, "Soup")
    client.post(f"/recipes/{rid}/edit", data={"name": "Soup", "ingredients": "broth\ncarrots"})
    page = client.get(f"/recipes/{rid}").data.decode()
    assert "broth\ncarrots" in page and "Instructions" not in page


def report_text(client):
    resp = client.get("/report.txt")
    assert resp.status_code == 200 and resp.mimetype == "text/plain"
    return resp.data.decode()


def test_report_when_empty(client):
    text = report_text(client)
    for cat in ("Breakfast", "Lunch", "Snack", "Dinner"):
        assert f"{cat}: 0\n" in text
        assert f"MAKE MORE {cat}: none left!" in text
    assert "THAW" not in text


def test_report_rules(client, app):
    oats = add_recipe(client, "Oats")
    soup = add_recipe(client, "Soup")
    stew = add_recipe(client, "Stew")
    # Breakfast: 1 meal, in the freezer -> make more (1 left) and thaw.
    make_batch(client, app, oats, category="Breakfast", Freezer=1)
    # Lunch: plenty in the fridge -> nothing.
    make_batch(client, app, soup, category="Lunch", Thawing=1, Syringes=1, Freezer=3)
    # Snack: 1 in the fridge, two recipes in the freezer -> thaw the one with more.
    make_batch(client, app, soup, category="Snack", Syringes=1, Freezer=1)
    make_batch(client, app, stew, category="Snack", Freezer=3)
    # Dinner: nothing -> urgent.

    text = report_text(client)
    assert "Breakfast: 1 (0 fridge, 1 freezer)" in text
    assert "Lunch: 5 (2 fridge, 3 freezer)" in text
    assert "Snack: 5 (1 fridge, 4 freezer)" in text
    assert "Dinner: 0\n" in text

    actions = [line for line in text.splitlines() if line and line[0] in "🚨⚠🧊"]
    assert actions == [
        "🚨 MAKE MORE Dinner: none left!",
        "⚠️ MAKE MORE Breakfast: only 1 left.",
        "🧊 THAW Breakfast: nothing in the fridge. Oats has 1 in the freezer.",
        "🧊 THAW Snack: only 1 in the fridge. Stew has 3 in the freezer.",
    ]
    assert "Lunch" not in "\n".join(actions)

    # The Stock page shows the same to-do list.
    page = client.get("/").data.decode()
    assert "To do today" in page and "THAW Snack: only 1 in the fridge." in page


def test_report_nothing_to_do(client, app):
    rid = add_recipe(client, "Soup")
    for cat in ("Breakfast", "Lunch", "Snack", "Dinner"):
        make_batch(client, app, rid, category=cat, Thawing=2, Freezer=2)
    assert "✅ Nothing to do today." in report_text(client)
    assert b"Nothing to do today." in client.get("/").data


class FakeNtfy:
    def __init__(self):
        self.sent = []

    def __call__(self, server, topic, title, message, **kwargs):
        self.sent.append({"server": server, "topic": topic, "title": title, "message": message, **kwargs})


@pytest.fixture
def ntfy(app):
    fake = FakeNtfy()
    app.config.update(NTFY_TOPIC="test-topic", SEND_NOTIFICATION=fake, PUBLIC_URL="https://example.test")
    return fake


def test_send_to_phone_button(client, ntfy):
    resp = client.post("/report/test", follow_redirects=True)
    assert b"Sent today&#39;s report to your phone" in resp.data
    (msg,) = ntfy.sent
    assert msg["topic"] == "test-topic" and msg["title"].startswith("Thomas's meals for ")
    assert "MAKE MORE Dinner: none left!" in msg["message"]
    assert not msg["message"].startswith("Thomas")  # title isn't repeated in the body
    assert msg["priority"] == 4 and msg["tags"] == ["rotating_light"]
    assert msg["click"] == "https://example.test"


def test_send_without_topic_explains(client, app):
    app.config["NTFY_TOPIC"] = None
    resp = client.post("/report/test", follow_redirects=True)
    assert b"NTFY_TOPIC is missing" in resp.data


def test_send_failure_is_reported(client, app, ntfy):
    def boom(*args, **kwargs):
        raise OSError("network down")
    app.config["SEND_NOTIFICATION"] = boom
    assert b"network down" in client.post("/report/test", follow_redirects=True).data
    resp = client.post("/report/send")
    assert resp.status_code == 500


def test_scheduled_send_once_per_day_after_report_hour(client, app, ntfy):
    app.config["REPORT_HOUR"] = 24  # never reached
    assert b"Too early" in client.post("/report/send").data
    assert ntfy.sent == []

    app.config["REPORT_HOUR"] = 0
    assert client.post("/report/send").data == b"Sent.\n"
    assert client.post("/report/send").data == b"Already sent today.\n"
    assert len(ntfy.sent) == 1


def test_quiet_priority_when_nothing_to_do(client, app, ntfy):
    rid = add_recipe(client, "Soup")
    for cat in ("Breakfast", "Lunch", "Snack", "Dinner"):
        make_batch(client, app, rid, category=cat, Thawing=2, Freezer=2)
    client.post("/report/test")
    assert ntfy.sent[0]["priority"] == 2 and "Nothing to do today" in ntfy.sent[0]["message"]
