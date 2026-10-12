"""The morning report: stock per category and what needs doing today.

Rules (Thomas has one meal of each category per day):
  - MAKE MORE when a category is down to 1 meal total; at 0 it's urgent.
  - THAW when a category has 1 or fewer meals in the fridge (thawing + syringes)
    and there are some in the freezer.
"""

FREEZER = "Freezer"


def build_report(db, today):
    rows = db.execute(
        """SELECT c.id AS category_id, c.name AS category, r.name AS recipe,
                  l.name AS location, s.quantity
           FROM categories c
           LEFT JOIN stock s ON s.category_id = c.id AND s.quantity > 0
           LEFT JOIN recipes r ON r.id = s.recipe_id
           LEFT JOIN locations l ON l.id = s.location_id
           ORDER BY c.sort_order, r.name"""
    ).fetchall()

    categories = {}
    for row in rows:
        cat = categories.setdefault(row["category_id"], {
            "name": row["category"], "total": 0, "fridge": 0, "freezer": 0, "freezer_by_recipe": {},
        })
        if row["quantity"] is None:
            continue
        cat["total"] += row["quantity"]
        if row["location"] == FREEZER:
            cat["freezer"] += row["quantity"]
            by_recipe = cat["freezer_by_recipe"]
            by_recipe[row["recipe"]] = by_recipe.get(row["recipe"], 0) + row["quantity"]
        else:
            cat["fridge"] += row["quantity"]

    urgent, make, thaw = [], [], []
    for cat in categories.values():
        name = cat["name"]
        if cat["total"] == 0:
            urgent.append(f"MAKE {name}: none left!")
        elif cat["total"] == 1:
            make.append(f"MAKE {name}: 1 left")
        if cat["fridge"] <= 1 and cat["freezer"] > 0:
            # Suggest the recipe with the most in the freezer.
            recipe = max(cat["freezer_by_recipe"].items(), key=lambda kv: (kv[1], kv[0]))[0]
            thaw.append(f"THAW {name}: {recipe}")

    return {
        "date": today,
        "categories": list(categories.values()),
        "actions": (
            [{"level": "urgent", "text": t} for t in urgent]
            + [{"level": "make", "text": t} for t in make]
            + [{"level": "thaw", "text": t} for t in thaw]
        ),
    }


ICONS = {"urgent": "🚨", "make": "⚠️", "thaw": "🧊"}


def format_title(report):
    return f"Thomas's meals · {report['date'].strftime('%a %b ')}{report['date'].day}"


def format_actions(report):
    """Just the to-do lines: what the phone notification shows."""
    if not report["actions"]:
        return "✅ Nothing to do today."
    return "\n".join(f"{ICONS[a['level']]} {a['text']}" for a in report["actions"])


def format_text(report):
    """Full plain-text report: title, stock per category, then the to-dos."""
    lines = [format_title(report), ""]
    for cat in report["categories"]:
        detail = f" ({cat['fridge']} fridge, {cat['freezer']} freezer)" if cat["total"] else ""
        lines.append(f"{cat['name']}: {cat['total']}{detail}")
    return "\n".join(lines) + "\n\n" + format_actions(report)
