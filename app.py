import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from psycopg.rows import dict_row

from report import build_report, format_actions, format_text, format_title

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

FEED_COUNTS = ("1", "2")  # syringes per feeding, chosen on Feeding Time!

KIND_LABELS = {"moved": "Moved", "used": "Used", "discarded": "Thrown out"}


def syringes(n):
    """Counts are in syringes of food: '1 syringe', '3 syringes'."""
    return f"{n} syringe{'' if n == 1 else 's'}"


class StockError(Exception):
    """A change would leave a location with fewer than zero syringes."""


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        # The session cookie only carries flash messages (there's no login),
        # so a fixed fallback key is fine.
        SECRET_KEY=os.environ.get("SECRET_KEY", "meal-tracker-flash-messages"),
        TIMEZONE=os.environ.get("APP_TIMEZONE", "America/New_York"),
        SESSION_COOKIE_SAMESITE="Lax",
        # Morning report push notifications (ntfy), published by the GitHub Actions
        # workflow from what /report/due returns. No topic = sending is off.
        NTFY_SERVER=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        NTFY_TOPIC=os.environ.get("NTFY_TOPIC"),
        REPORT_HOUR=int(os.environ.get("REPORT_HOUR", "7")),
        # Render sets this; tapping the notification opens the app.
        PUBLIC_URL=os.environ.get("RENDER_EXTERNAL_URL"),
    )
    if config:
        app.config.update(config)

    if not app.config["DATABASE_URL"]:
        raise RuntimeError("Missing required setting: DATABASE_URL")

    app.config["TZ"] = ZoneInfo(app.config["TIMEZONE"])
    init_db(app.config["DATABASE_URL"])

    app.teardown_appcontext(close_db)
    app.jinja_env.filters["localtime"] = format_local_time
    register_routes(app)
    return app


# --- database -------------------------------------------------------------


def init_db(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.transaction():
            # Serialize schema setup if several workers start at once.
            conn.execute("SELECT pg_advisory_xact_lock(4242)")
            conn.execute(SCHEMA_PATH.read_text())


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(
            current_app.config["DATABASE_URL"], autocommit=True, row_factory=dict_row
        )
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_locations(db):
    return db.execute("SELECT * FROM locations ORDER BY sort_order").fetchall()


def get_categories(db):
    return db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()


def get_recipes(db, include_archived=False):
    sql = "SELECT * FROM recipes"
    if not include_archived:
        sql += " WHERE NOT archived"
    return db.execute(sql + " ORDER BY lower(name)").fetchall()


def get_stock_map(db):
    """{(recipe_id, category_id): {location_id: syringes}}, only for food on hand."""
    stock = {}
    for row in db.execute(
        "SELECT recipe_id, category_id, location_id, quantity FROM stock WHERE quantity > 0"
    ):
        stock.setdefault((row["recipe_id"], row["category_id"]), {})[row["location_id"]] = row["quantity"]
    return stock


def recipe_totals(stock):
    """{recipe_id: total syringes across all categories and locations}"""
    totals = {}
    for (recipe_id, _), counts in stock.items():
        totals[recipe_id] = totals.get(recipe_id, 0) + sum(counts.values())
    return totals


def adjust_stock(db, recipe_id, category_id, location_id, delta):
    key = (recipe_id, category_id, location_id)
    if delta < 0:
        row = db.execute(
            """SELECT quantity FROM stock
               WHERE recipe_id = %s AND category_id = %s AND location_id = %s FOR UPDATE""",
            key,
        ).fetchone()
        have = row["quantity"] if row else 0
        if have < -delta:
            names = db.execute(
                """SELECT r.name AS recipe, c.name AS category, l.name AS location
                   FROM recipes r, categories c, locations l
                   WHERE r.id = %s AND c.id = %s AND l.id = %s""",
                key,
            ).fetchone() or {"recipe": "that recipe", "category": "that category", "location": "that spot"}
            raise StockError(
                f"There {'is' if have == 1 else 'are'} only {syringes(have)} of {names['recipe']} "
                f"({names['category']}) in {names['location']}."
            )
        db.execute(
            """UPDATE stock SET quantity = quantity + %s
               WHERE recipe_id = %s AND category_id = %s AND location_id = %s""",
            (delta, *key),
        )
        return
    db.execute(
        """INSERT INTO stock (recipe_id, category_id, location_id, quantity) VALUES (%s, %s, %s, %s)
           ON CONFLICT (recipe_id, category_id, location_id)
           DO UPDATE SET quantity = stock.quantity + EXCLUDED.quantity""",
        (*key, delta),
    )


def apply_movement(db, recipe_id, category_id, from_location_id, to_location_id, quantity):
    if from_location_id:
        adjust_stock(db, recipe_id, category_id, from_location_id, -quantity)
    if to_location_id:
        adjust_stock(db, recipe_id, category_id, to_location_id, quantity)


# --- helpers --------------------------------------------------------------


def format_local_time(value, fmt="%b %-d, %-I:%M %p"):
    if value is None:
        return ""
    local = value.astimezone(current_app.config["TZ"])
    if os.name == "nt":  # Windows strftime has no %-d / %-I
        fmt = fmt.replace("%-", "%#")
    return local.strftime(fmt)


def today_local():
    return datetime.now(current_app.config["TZ"]).date()


def parse_int(value, field, minimum=0):
    try:
        number = int((value or "0").strip())
    except ValueError:
        raise ValueError(f"{field} must be a whole number.")
    if number < minimum:
        raise ValueError(f"{field} must be at least {minimum}.")
    return number


def parse_date(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Pick the date it was made.")


# --- routes ---------------------------------------------------------------


def register_routes(app):
    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/")
    def inventory():
        db = get_db()
        locations = get_locations(db)
        stock = get_stock_map(db)
        recipes = get_recipes(db, include_archived=True)  # already sorted by name
        sections = []
        for category in get_categories(db):
            rows = [
                {"recipe": r, "counts": stock[(r["id"], category["id"])],
                 "total": sum(stock[(r["id"], category["id"])].values())}
                for r in recipes
                if (r["id"], category["id"]) in stock
            ]
            sections.append({
                "category": category,
                "rows": rows,
                "total": sum(row["total"] for row in rows),
            })
        return render_template(
            "inventory.html",
            locations=locations,
            sections=sections,
            grand_total=sum(s["total"] for s in sections),
            report=build_report(db, today_local()),
        )

    @app.get("/report.txt")
    def report_text():
        # Plain-text morning report, for the daily text/notification to send.
        text = format_text(build_report(get_db(), today_local()))
        return text, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.post("/report/due")
    def report_due():
        """Called by the GitHub Actions scheduler, which then publishes to ntfy itself.

        Sending from GitHub's rotating runner IPs avoids ntfy.sh's per-IP limit, which
        Render's shared outbound IPs can exhaust. The scheduler fires at two UTC times
        so one of them lands on REPORT_HOUR in both daylight and standard time; the
        report is due once per day, at or after that hour, until /report/sent is called.

        ?test=1 (the "Test report" workflow) is always due and marked as a test; it
        doesn't call /report/sent, so the real morning report is unaffected.
        """
        db = get_db()
        now = datetime.now(app.config["TZ"])
        if not app.config["NTFY_TOPIC"]:
            return {"due": False, "reason": "NTFY_TOPIC is not set."}
        if request.args.get("test") == "1":
            payload = notification_payload(db)
            payload["title"] += " (test)"
            return {"due": True, "server": app.config["NTFY_SERVER"], "payload": payload}
        if now.hour < app.config["REPORT_HOUR"]:
            return {"due": False, "reason": f"Too early ({now:%H:%M})."}
        if get_state(db, "report_last_sent") == now.date().isoformat():
            return {"due": False, "reason": "Already sent today."}
        return {"due": True, "server": app.config["NTFY_SERVER"], "payload": notification_payload(db)}

    @app.post("/report/sent")
    def report_sent():
        db = get_db()
        db.execute(
            """INSERT INTO app_state (key, value) VALUES ('report_last_sent', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (today_local().isoformat(),),
        )
        return {"ok": True}

    @app.route("/fed", methods=["GET", "POST"])
    def fed_meal():
        """'Thomas just had this': pick 1 or 2 syringes, then tap the meal. Uses that
        many from Fridge (in syringes)."""
        db = get_db()
        filled = db.execute("SELECT * FROM locations WHERE short_name = 'Syringes'").fetchone()

        if request.method == "POST":
            try:
                count = request.form.get("count", "1")
                if count not in FEED_COUNTS:
                    raise ValueError("Pick 1 or 2 syringes.")
                count = int(count)
                recipe_id, category_id = parse_item(request.form.get("meal"))
                with db.transaction():
                    apply_movement(db, recipe_id, category_id, filled["id"], None, count)
                    db.execute(
                        """INSERT INTO movements (recipe_id, category_id, kind, from_location_id, quantity)
                           VALUES (%s, %s, 'used', %s, %s)""",
                        (recipe_id, category_id, filled["id"], count),
                    )
            except (ValueError, StockError) as e:
                flash(str(e), "error")
                return redirect(url_for("fed_meal"))
            n = db.execute(
                """SELECT r.name AS recipe, c.name AS category, COALESCE(s.quantity, 0) AS left
                   FROM recipes r JOIN categories c ON c.id = %s
                   LEFT JOIN stock s ON s.recipe_id = r.id AND s.category_id = c.id AND s.location_id = %s
                   WHERE r.id = %s""",
                (category_id, filled["id"], recipe_id),
            ).fetchone()
            flash(f"Fed {syringes(count)} of {n['recipe']} ({n['category']}). {n['left']} filled left.", "success")
            return redirect(url_for("inventory"))

        meals = db.execute(
            """SELECT s.recipe_id, s.category_id, s.quantity, r.name AS recipe, c.name AS category
               FROM stock s
               JOIN recipes r ON r.id = s.recipe_id
               JOIN categories c ON c.id = s.category_id
               WHERE s.location_id = %s AND s.quantity > 0
               ORDER BY c.sort_order, lower(r.name)""",
            (filled["id"],),
        ).fetchall()
        return render_template("fed.html", meals=meals, counts=FEED_COUNTS)

    @app.route("/make", methods=["GET", "POST"])
    def make_batch():
        db = get_db()
        locations = get_locations(db)
        categories = get_categories(db)
        recipes = get_recipes(db)
        form = request.form

        if request.method == "POST":
            try:
                recipe_id = parse_int(form.get("recipe_id"), "Recipe", minimum=1)
                category_id = parse_int(form.get("category_id"), "Category")
                if category_id not in {c["id"] for c in categories}:
                    raise ValueError("Pick breakfast, lunch, snack, or dinner.")
                made_on = parse_date(form.get("made_on"))
                split = {
                    loc["id"]: parse_int(form.get(f"qty_{loc['id']}"), loc["name"])
                    for loc in locations
                }
                if sum(split.values()) == 0:
                    raise ValueError("Enter how many syringes went into at least one spot.")
            except ValueError as e:
                flash(str(e), "error")
            else:
                with db.transaction():
                    batch_id = db.execute(
                        """INSERT INTO batches (recipe_id, category_id, made_on, notes)
                           VALUES (%s, %s, %s, %s) RETURNING id""",
                        (recipe_id, category_id, made_on, form.get("notes", "").strip()),
                    ).fetchone()["id"]
                    for loc_id, qty in split.items():
                        if qty:
                            apply_movement(db, recipe_id, category_id, None, loc_id, qty)
                            db.execute(
                                """INSERT INTO movements
                                       (recipe_id, category_id, batch_id, kind, to_location_id, quantity)
                                   VALUES (%s, %s, %s, 'made', %s, %s)""",
                                (recipe_id, category_id, batch_id, loc_id, qty),
                            )
                name = next((r["name"] for r in recipes if r["id"] == recipe_id), "batch")
                category = next(c["name"] for c in categories if c["id"] == category_id)
                flash(f"Added {syringes(sum(split.values()))} of {name} ({category}).", "success")
                return redirect(url_for("inventory"))

        return render_template(
            "make.html",
            recipes=recipes,
            categories=categories,
            locations=locations,
            form=form,
            selected_recipe=form.get("recipe_id") or request.args.get("recipe_id", ""),
            today=today_local().isoformat(),
        )

    @app.route("/move", methods=["GET", "POST"])
    def move():
        db = get_db()
        locations = get_locations(db)
        location_ids = {loc["id"] for loc in locations}
        categories = get_categories(db)
        recipes = get_recipes(db, include_archived=True)
        stock = get_stock_map(db)
        values = request.form if request.method == "POST" else request.args

        if request.method == "POST":
            try:
                kind = values.get("kind")
                if kind not in KIND_LABELS:
                    raise ValueError("Pick what happened.")
                recipe_id, category_id = parse_item(values.get("meal"))
                quantity = parse_int(values.get("quantity"), "Number of syringes", minimum=1)
                from_id = parse_int(values.get("from_location_id"), "From", minimum=1)
                to_id = None
                if kind == "moved":
                    to_id = parse_int(values.get("to_location_id"), "To", minimum=1)
                    if to_id == from_id:
                        raise ValueError("Pick two different spots.")
                if from_id not in location_ids or (to_id and to_id not in location_ids):
                    raise ValueError("Unknown location.")
                with db.transaction():
                    apply_movement(db, recipe_id, category_id, from_id, to_id, quantity)
                    db.execute(
                        """INSERT INTO movements
                               (recipe_id, category_id, kind, from_location_id, to_location_id, quantity)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (recipe_id, category_id, kind, from_id, to_id, quantity),
                    )
            except (ValueError, StockError) as e:
                flash(str(e), "error")
            else:
                flash(describe_change(db, kind, quantity, recipe_id, category_id, from_id, to_id), "success")
                return redirect(url_for("inventory"))

        # One choice per recipe + category on hand, grouped by category.
        recipe_names = {r["id"]: r["name"] for r in recipes}
        groups = []
        for c in categories:
            items = [
                {"key": f"{r_id}:{c_id}", "label": recipe_names[r_id]}
                for (r_id, c_id) in stock
                if c_id == c["id"]
            ]
            if items:
                groups.append({"category": c["name"], "items": sorted(items, key=lambda i: i["label"].lower())})
        selected = values.get("meal") or f"{values.get('recipe_id')}:{values.get('category_id')}"
        stock_json = {f"{r}:{c}": {str(l): q for l, q in locs.items()} for (r, c), locs in stock.items()}
        return render_template(
            "move.html",
            groups=groups,
            selected=selected,
            locations=locations,
            values=values,
            stock_json=stock_json,
        )

    @app.get("/recipes")
    def recipes():
        db = get_db()
        totals = recipe_totals(get_stock_map(db))
        all_recipes = get_recipes(db, include_archived=True)
        return render_template(
            "recipes.html",
            active=[r for r in all_recipes if not r["archived"]],
            archived=[r for r in all_recipes if r["archived"]],
            totals={r["id"]: totals.get(r["id"], 0) for r in all_recipes},
        )

    @app.route("/recipes/new", methods=["GET", "POST"])
    def new_recipe():
        if request.method == "POST":
            recipe_id = save_recipe(None)
            if recipe_id:
                flash("Recipe added.", "success")
                return redirect(url_for("recipe_detail", recipe_id=recipe_id))
        return render_template("recipe_form.html", recipe=request.form, is_new=True)

    @app.route("/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
    def edit_recipe(recipe_id):
        recipe = load_recipe(recipe_id)
        if request.method == "POST":
            if save_recipe(recipe_id):
                flash("Recipe saved.", "success")
                return redirect(url_for("recipe_detail", recipe_id=recipe_id))
            recipe = {**request.form, "id": recipe_id, "archived": bool(request.form.get("archived"))}
        return render_template("recipe_form.html", recipe=recipe, is_new=False)

    @app.get("/recipes/<int:recipe_id>")
    def recipe_detail(recipe_id):
        db = get_db()
        recipe = load_recipe(recipe_id)
        batches = db.execute(
            """SELECT b.*, c.name AS category, COALESCE(SUM(m.quantity), 0) AS meals
               FROM batches b
               JOIN categories c ON c.id = b.category_id
               LEFT JOIN movements m ON m.batch_id = b.id AND m.kind = 'made'
               WHERE b.recipe_id = %s
               GROUP BY b.id, c.name
               ORDER BY b.made_on DESC, b.id DESC""",
            (recipe_id,),
        ).fetchall()
        # Syringes of this recipe on hand, summed across categories.
        counts = {}
        for (r_id, _), locs in get_stock_map(db).items():
            if r_id == recipe_id:
                for loc_id, qty in locs.items():
                    counts[loc_id] = counts.get(loc_id, 0) + qty
        return render_template(
            "recipe_detail.html",
            recipe=recipe,
            batches=batches,
            locations=get_locations(db),
            counts=counts,
            total=sum(counts.values()),
        )

    @app.get("/history")
    def history():
        db = get_db()
        recipes = get_recipes(db, include_archived=True)
        recipe = None
        if request.args.get("recipe_id", "").isdigit():
            recipe = next((r for r in recipes if r["id"] == int(request.args["recipe_id"])), None)

        movements = db.execute(
            """SELECT m.*, r.name AS recipe, c.name AS category, b.made_on,
                      lf.short_name AS from_name, lt.short_name AS to_name
               FROM movements m
               JOIN recipes r ON r.id = m.recipe_id
               JOIN categories c ON c.id = m.category_id
               LEFT JOIN batches b ON b.id = m.batch_id
               LEFT JOIN locations lf ON lf.id = m.from_location_id
               LEFT JOIN locations lt ON lt.id = m.to_location_id
               WHERE %(recipe_id)s::int IS NULL OR m.recipe_id = %(recipe_id)s
               ORDER BY m.created_at DESC, m.id DESC
               LIMIT 200""",
            {"recipe_id": recipe["id"] if recipe else None},
        ).fetchall()

        last_made = None
        if recipe:
            last_made = db.execute(
                "SELECT max(made_on) AS made_on FROM batches WHERE recipe_id = %s", (recipe["id"],)
            ).fetchone()["made_on"]
        return render_template(
            "history.html",
            movements=movements,
            recipes=recipes,
            recipe=recipe,
            last_made=last_made,
            days_ago=(today_local() - last_made).days if last_made else None,
        )

    @app.post("/history/<int:movement_id>/undo")
    def undo_movement(movement_id):
        db = get_db()
        try:
            with db.transaction():
                m = db.execute(
                    "SELECT * FROM movements WHERE id = %s FOR UPDATE", (movement_id,)
                ).fetchone()
                if m is None:
                    abort(404)
                # Reverse the change: put the syringes back where they came from.
                apply_movement(
                    db, m["recipe_id"], m["category_id"], m["to_location_id"], m["from_location_id"], m["quantity"]
                )
                db.execute("DELETE FROM movements WHERE id = %s", (movement_id,))
                if m["batch_id"]:
                    db.execute(
                        """DELETE FROM batches b WHERE b.id = %s
                           AND NOT EXISTS (SELECT 1 FROM movements WHERE batch_id = b.id)""",
                        (m["batch_id"],),
                    )
        except StockError as e:
            flash(f"Can't undo that: {e} Some of those syringes have already been moved or used.", "error")
        else:
            flash("Undone.", "success")
        # Stay on the same filtered view.
        recipe_filter = request.form.get("recipe_id", "")
        return redirect(url_for("history", recipe_id=recipe_filter) if recipe_filter.isdigit() else url_for("history"))


def parse_item(value):
    """'recipe_id:category_id' from the Move form's meal picker."""
    try:
        recipe_id, category_id = (int(part) for part in (value or "").split(":"))
    except ValueError:
        raise ValueError("Pick which meal.")
    return recipe_id, category_id


def describe_change(db, kind, quantity, recipe_id, category_id, from_id, to_id):
    n = db.execute(
        """SELECT r.name AS recipe, c.name AS category, lf.name AS from_name, lt.name AS to_name
           FROM recipes r
           JOIN categories c ON c.id = %s
           JOIN locations lf ON lf.id = %s
           LEFT JOIN locations lt ON lt.id = %s
           WHERE r.id = %s""",
        (category_id, from_id, to_id, recipe_id),
    ).fetchone()
    what = f"{syringes(quantity)} of {n['recipe']} ({n['category']})"
    if kind == "moved":
        return f"Moved {what} from {n['from_name']} to {n['to_name']}."
    return f"{KIND_LABELS[kind]}: {what} from {n['from_name']}."


def load_recipe(recipe_id):
    recipe = get_db().execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,)).fetchone()
    if recipe is None:
        abort(404)
    return recipe


def save_recipe(recipe_id):
    """Insert or update from request.form. Returns the id, or None after flashing an error."""
    form = request.form
    name = form.get("name", "").strip()
    if not name:
        flash("Give the recipe a name.", "error")
        return None
    fields = (name, form.get("ingredients", "").strip())
    db = get_db()
    try:
        if recipe_id is None:
            return db.execute(
                "INSERT INTO recipes (name, ingredients) VALUES (%s, %s) RETURNING id",
                fields,
            ).fetchone()["id"]
        db.execute(
            "UPDATE recipes SET name = %s, ingredients = %s, archived = %s WHERE id = %s",
            (*fields, bool(form.get("archived")), recipe_id),
        )
        return recipe_id
    except psycopg.errors.UniqueViolation:
        flash(f"There's already a recipe called “{name}”.", "error")
        return None


def get_state(db, key):
    row = db.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def notification_payload(db):
    """Today's report as an ntfy JSON message: just the to-dos, so it fits on the lock screen."""
    config = current_app.config
    report = build_report(db, today_local())
    levels = {a["level"] for a in report["actions"]}
    # No ntfy tags: the app shows them as emoji in front of the title.
    priority = 4 if "urgent" in levels else 3 if levels else 2
    payload = {
        "topic": config["NTFY_TOPIC"],
        "title": format_title(report),
        "message": format_actions(report),
        "priority": priority,
    }
    if config["PUBLIC_URL"]:
        payload["click"] = config["PUBLIC_URL"]
    return payload
