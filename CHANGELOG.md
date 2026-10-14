# Changelog

All notable changes to Thomas Tube Tracking.

## 1.1 (2026-10-14)

Tweaks from the first two days of real use.

### Changed
- Food is counted in **syringes** instead of meals, everywhere in the app and the
  morning report. Locations and existing numbers are unchanged.
- "Low" now means less than two days' worth: the morning report flags MAKE when a
  category has fewer than 4 syringes (still urgent at 0) and THAW when the fridge has
  fewer than 4 and the freezer has some.
- **Feeding Time!** asks how many syringes were fed (1 or 2) before you tap the meal,
  and takes that many from Fridge (in syringes).

### Added
- History shows **Fed to Thomas: N syringes total** when you pick a recipe, next to
  when it was last made.
- The morning notification ends with **"N syringes currently thawing"** when anything
  is still thawing (left out at 0). On days with nothing to make or thaw, the
  "✅ Nothing to do today." all-clear still shows above it. This line is only in the
  morning report, not on the Stock page.

## 1.0 (2026-10-12)

First release: the version shown on launch night.

### Added
- **Stock** page: syringes of each recipe by category (Breakfast, Lunch, Snack,
  Dinner) and location (Freezer, Fridge (thawing), Fridge (in syringes)), with
  per-category totals. Tap a number to move or use it.
- **Feeding Time!**: one tap to log that Thomas had a meal.
- **Made a batch**: log a recipe, the category it's for, the date, and how much went
  into each location.
- **Move / Use**: move food between locations, mark it used, or throw it out.
- **Recipes**: name and ingredients, batch history, and archiving.
- **History**: every change with undo, filterable by recipe, showing when it was last
  made.
- **To do today** card on the Stock page and a **morning report** pushed to phones
  with [ntfy](https://ntfy.sh) at 7 AM Eastern: MAKE (low or out) and THAW (fridge
  running low, freezer has some). Titled "Thomas Meal Report / Mon, Oct 12".
- **Test report** GitHub workflow to send the report on demand.
- Thomas Tube Tracking name, G-tube button logo, navy and coral theme with dark mode,
  three-bar menu, and a home-screen icon.
- Runs free on Render (web app), Neon (database), and GitHub Actions (daily report).
