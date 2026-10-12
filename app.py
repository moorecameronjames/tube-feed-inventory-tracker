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

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

KIND_LABELS = {"moved": "Moved", "used": "Used", "discarded": "Thrown out"}


class StockError(Exception):
    """A change would leave a location with fewer than zero meals."""


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        # The session cookie only carries flash messages (there's no login),
        # so a fixed fallback key is fine.
        SECRET_KEY=os.environ.get("SECRET_KEY", "meal-tracker-flash-messages"),
        TIMEZONE=os.environ.get("APP_TIMEZONE", "America/New_York"),
        SESSION_COOKIE_SAMESITE="Lax",
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


def get_recipes(db, include_archived=False):
    sql = "SELECT * FROM recipes"
    if not include_archived:
        sql += " WHERE NOT archived"
    return db.execute(sql + " ORDER BY lower(name)").fetchall()


def get_stock_map(db):
    """{recipe_id: {location_id: quantity}}"""
    stock = {}
    for row in db.execute("SELECT recipe_id, location_id, quantity FROM stock"):
        stock.setdefault(row["recipe_id"], {})[row["location_id"]] = row["quantity"]
    return stock


def adjust_stock(db, recipe_id, location_id, delta):
    if delta < 0:
        row = db.execute(
            "SELECT quantity FROM stock WHERE recipe_id = %s AND location_id = %s FOR UPDATE",
            (recipe_id, location_id),
        ).fetchone()
        have = row["quantity"] if row else 0
        if have < -delta:
            names = db.execute(
                """SELECT r.name AS recipe, l.name AS location
                   FROM recipes r, locations l WHERE r.id = %s AND l.id = %s""",
                (recipe_id, location_id),
            ).fetchone() or {"recipe": "that recipe", "location": "that spot"}
            raise StockError(
                f"There {'is' if have == 1 else 'are'} only {have} {names['recipe']} in {names['location']}."
            )
        db.execute(
            "UPDATE stock SET quantity = quantity + %s WHERE recipe_id = %s AND location_id = %s",
            (delta, recipe_id, location_id),
        )
        return
    db.execute(
        """INSERT INTO stock (recipe_id, location_id, quantity) VALUES (%s, %s, %s)
           ON CONFLICT (recipe_id, location_id)
           DO UPDATE SET quantity = stock.quantity + EXCLUDED.quantity""",
        (recipe_id, location_id, delta),
    )


def apply_movement(db, recipe_id, from_location_id, to_location_id, quantity):
    if from_location_id:
        adjust_stock(db, recipe_id, from_location_id, -quantity)
    if to_location_id:
        adjust_stock(db, recipe_id, to_location_id, quantity)


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
        rows = []
        totals = {loc["id"]: 0 for loc in locations}
        for recipe in get_recipes(db, include_archived=True):
            counts = stock.get(recipe["id"], {})
            total = sum(counts.values())
            if recipe["archived"] and total == 0:
                continue
            for loc_id, qty in counts.items():
                totals[loc_id] += qty
            rows.append({"recipe": recipe, "counts": counts, "total": total})
        return render_template(
            "inventory.html",
            locations=locations,
            rows=rows,
            totals=totals,
            grand_total=sum(totals.values()),
        )

    @app.route("/make", methods=["GET", "POST"])
    def make_batch():
        db = get_db()
        locations = get_locations(db)
        recipes = get_recipes(db)
        form = request.form

        if request.method == "POST":
            try:
                recipe_id = parse_int(form.get("recipe_id"), "Recipe", minimum=1)
                made_on = parse_date(form.get("made_on"))
                split = {
                    loc["id"]: parse_int(form.get(f"qty_{loc['id']}"), loc["name"])
                    for loc in locations
                }
                if sum(split.values()) == 0:
                    raise ValueError("Enter how many meals went into at least one spot.")
            except ValueError as e:
                flash(str(e), "error")
            else:
                with db.transaction():
                    batch_id = db.execute(
                        "INSERT INTO batches (recipe_id, made_on, notes) VALUES (%s, %s, %s) RETURNING id",
                        (recipe_id, made_on, form.get("notes", "").strip()),
                    ).fetchone()["id"]
                    for loc_id, qty in split.items():
                        if qty:
                            apply_movement(db, recipe_id, None, loc_id, qty)
                            db.execute(
                                """INSERT INTO movements (recipe_id, batch_id, kind, to_location_id, quantity)
                                   VALUES (%s, %s, 'made', %s, %s)""",
                                (recipe_id, batch_id, loc_id, qty),
                            )
                name = next((r["name"] for r in recipes if r["id"] == recipe_id), "batch")
                flash(f"Added {sum(split.values())} meals of {name}.", "success")
                return redirect(url_for("inventory"))

        return render_template(
            "make.html",
            recipes=recipes,
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
        recipes = get_recipes(db, include_archived=True)
        stock = get_stock_map(db)
        values = request.form if request.method == "POST" else request.args

        if request.method == "POST":
            try:
                kind = values.get("kind")
                if kind not in KIND_LABELS:
                    raise ValueError("Pick what happened.")
                recipe_id = parse_int(values.get("recipe_id"), "Recipe", minimum=1)
                quantity = parse_int(values.get("quantity"), "Number of meals", minimum=1)
                from_id = parse_int(values.get("from_location_id"), "From", minimum=1)
                to_id = None
                if kind == "moved":
                    to_id = parse_int(values.get("to_location_id"), "To", minimum=1)
                    if to_id == from_id:
                        raise ValueError("Pick two different spots.")
                if from_id not in location_ids or (to_id and to_id not in location_ids):
                    raise ValueError("Unknown location.")
                with db.transaction():
                    apply_movement(db, recipe_id, from_id, to_id, quantity)
                    db.execute(
                        """INSERT INTO movements (recipe_id, kind, from_location_id, to_location_id, quantity)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (recipe_id, kind, from_id, to_id, quantity),
                    )
            except (ValueError, StockError) as e:
                flash(str(e), "error")
            else:
                flash(describe_change(kind, quantity, recipes, locations, recipe_id, from_id, to_id), "success")
                return redirect(url_for("inventory"))

        stock_json = {str(r): {str(l): q for l, q in locs.items()} for r, locs in stock.items()}
        return render_template(
            "move.html",
            recipes=[r for r in recipes if not r["archived"] or sum(stock.get(r["id"], {}).values())],
            locations=locations,
            values=values,
            stock_json=stock_json,
        )

    @app.get("/recipes")
    def recipes():
        db = get_db()
        stock = get_stock_map(db)
        all_recipes = get_recipes(db, include_archived=True)
        totals = {r["id"]: sum(stock.get(r["id"], {}).values()) for r in all_recipes}
        return render_template(
            "recipes.html",
            active=[r for r in all_recipes if not r["archived"]],
            archived=[r for r in all_recipes if r["archived"]],
            totals=totals,
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
            """SELECT b.*, COALESCE(SUM(m.quantity), 0) AS meals
               FROM batches b
               LEFT JOIN movements m ON m.batch_id = b.id AND m.kind = 'made'
               WHERE b.recipe_id = %s
               GROUP BY b.id
               ORDER BY b.made_on DESC, b.id DESC""",
            (recipe_id,),
        ).fetchall()
        counts = get_stock_map(db).get(recipe_id, {})
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
            """SELECT m.*, r.name AS recipe, b.made_on,
                      lf.short_name AS from_name, lt.short_name AS to_name
               FROM movements m
               JOIN recipes r ON r.id = m.recipe_id
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
                # Reverse the change: put meals back where they came from.
                apply_movement(db, m["recipe_id"], m["to_location_id"], m["from_location_id"], m["quantity"])
                db.execute("DELETE FROM movements WHERE id = %s", (movement_id,))
                if m["batch_id"]:
                    db.execute(
                        """DELETE FROM batches b WHERE b.id = %s
                           AND NOT EXISTS (SELECT 1 FROM movements WHERE batch_id = b.id)""",
                        (m["batch_id"],),
                    )
        except StockError as e:
            flash(f"Can't undo that: {e} Some of those meals have already been moved or used.", "error")
        else:
            flash("Undone.", "success")
        # Stay on the same filtered view.
        recipe_filter = request.form.get("recipe_id", "")
        return redirect(url_for("history", recipe_id=recipe_filter) if recipe_filter.isdigit() else url_for("history"))


def describe_change(kind, quantity, recipes, locations, recipe_id, from_id, to_id):
    recipe = next(r["name"] for r in recipes if r["id"] == recipe_id)
    names = {loc["id"]: loc["name"] for loc in locations}
    meals = f"{quantity} {recipe}"
    if kind == "moved":
        return f"Moved {meals} from {names[from_id]} to {names[to_id]}."
    return f"{KIND_LABELS[kind]}: {meals} from {names[from_id]}."


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
