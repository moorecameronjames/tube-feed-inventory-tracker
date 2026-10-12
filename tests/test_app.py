import re

import pgserver
import psycopg
import pytest

from app import create_app

PASSWORD = "test-password"


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture
def app(pg_uri):
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return create_app(
        {"DATABASE_URL": pg_uri, "APP_PASSWORD": PASSWORD, "SECRET_KEY": "test", "TESTING": True}
    )


@pytest.fixture
def client(app):
    client = app.test_client()
    client.post("/login", data={"password": PASSWORD})
    return client


def location_ids(app):
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        return {short: id for id, short in conn.execute("SELECT id, short_name FROM locations")}


def stock(app, recipe_id):
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        rows = conn.execute(
            """SELECT l.short_name, s.quantity FROM stock s JOIN locations l ON l.id = s.location_id
               WHERE s.recipe_id = %s""",
            (recipe_id,),
        )
        return {name: qty for name, qty in rows if qty}


def add_recipe(client, name="Chicken & Rice"):
    resp = client.post("/recipes/new", data={"name": name, "ingredients": "chicken\nrice"})
    assert resp.status_code == 302
    return int(re.search(r"/recipes/(\d+)", resp.headers["Location"]).group(1))


def make_batch(client, app, recipe_id, **split):
    locs = location_ids(app)
    data = {"recipe_id": recipe_id, "made_on": "2026-10-12"}
    data.update({f"qty_{locs[name]}": qty for name, qty in split.items()})
    return client.post("/make", data=data, follow_redirects=True)


def test_requires_login(app):
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.post("/login", data={"password": "nope"}).status_code == 200
    assert client.get("/").status_code == 302
    resp = client.post("/login", data={"password": PASSWORD, "next": "https://evil.example"})
    assert resp.headers["Location"] == "/"
    assert client.get("/").status_code == 200


def test_healthz_is_public(app):
    assert app.test_client().get("/healthz").data == b"ok"


def test_make_batch_splits_across_locations(client, app):
    rid = add_recipe(client)
    resp = make_batch(client, app, rid, Freezer=3, Thawing=3)
    assert b"Added 6 meals of Chicken &amp; Rice" in resp.data
    assert stock(app, rid) == {"Freezer": 3, "Thawing": 3}
    # Second batch adds on top.
    make_batch(client, app, rid, Freezer=2)
    assert stock(app, rid) == {"Freezer": 5, "Thawing": 3}


def test_make_batch_needs_some_meals(client, app):
    rid = add_recipe(client)
    resp = make_batch(client, app, rid)
    assert b"at least one spot" in resp.data
    assert stock(app, rid) == {}


def test_move_use_and_discard(client, app):
    rid = add_recipe(client)
    locs = location_ids(app)
    make_batch(client, app, rid, Freezer=4)

    client.post("/move", data={"kind": "moved", "recipe_id": rid, "quantity": 2,
                               "from_location_id": locs["Freezer"], "to_location_id": locs["Thawing"]})
    assert stock(app, rid) == {"Freezer": 2, "Thawing": 2}

    client.post("/move", data={"kind": "moved", "recipe_id": rid, "quantity": 1,
                               "from_location_id": locs["Thawing"], "to_location_id": locs["Syringes"]})
    client.post("/move", data={"kind": "used", "recipe_id": rid, "quantity": 1,
                               "from_location_id": locs["Syringes"], "to_location_id": locs["Freezer"]})
    client.post("/move", data={"kind": "discarded", "recipe_id": rid, "quantity": 1,
                               "from_location_id": locs["Freezer"]})
    assert stock(app, rid) == {"Freezer": 1, "Thawing": 1}


def test_cannot_move_more_than_available(client, app):
    rid = add_recipe(client)
    locs = location_ids(app)
    make_batch(client, app, rid, Freezer=1)
    resp = client.post("/move", data={"kind": "used", "recipe_id": rid, "quantity": 2,
                                      "from_location_id": locs["Freezer"]})
    assert b"only 1 Chicken &amp; Rice in Freezer" in resp.data
    resp = client.post("/move", data={"kind": "used", "recipe_id": rid, "quantity": 1,
                                      "from_location_id": locs["Thawing"]})
    assert b"only 0" in resp.data
    assert stock(app, rid) == {"Freezer": 1}


def test_inventory_and_history_pages(client, app):
    rid = add_recipe(client, "Beef Stew")
    make_batch(client, app, rid, Freezer=3, Syringes=2)
    page = client.get("/").data.decode()
    assert "Beef Stew" in page and "Total meals" in page
    history = client.get("/history").data.decode()
    assert "Made</strong> 3 Beef Stew" in history
    assert client.get(f"/recipes/{rid}").status_code == 200
    assert client.get(f"/recipes/{rid}/edit").status_code == 200
    assert client.get("/move").status_code == 200
    assert client.get("/make").status_code == 200


def test_undo(client, app):
    rid = add_recipe(client)
    locs = location_ids(app)
    make_batch(client, app, rid, Freezer=2)
    client.post("/move", data={"kind": "moved", "recipe_id": rid, "quantity": 2,
                               "from_location_id": locs["Freezer"], "to_location_id": locs["Thawing"]})

    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        (made_id,), (move_id,) = conn.execute("SELECT id FROM movements ORDER BY id").fetchall()

    # Can't undo the batch while its meals have been moved out of the freezer.
    resp = client.post(f"/history/{made_id}/undo", follow_redirects=True)
    assert b"Can&#39;t undo" in resp.data
    client.post(f"/history/{move_id}/undo")
    assert stock(app, rid) == {"Freezer": 2}
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
