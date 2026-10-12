# Thomas Tube Tracking

<img src="static/logo.svg" alt="Thomas Tube Tracking" width="320">

Tracks home-made tube-feed recipes and how many meals of each are in the
**Freezer**, **Fridge (thawing)**, and **Fridge (in syringes)**.

- **Stock**: counts per recipe and location. Tap a number to move or use those meals.
- **Made**: log a batch and split it across locations ("3 fridge, 3 freezer").
- **Move / Use**: move meals between locations, mark them used, or throw them out.
- **Recipes**: ingredients and batch history.
- **History**: every change, with undo.

Python + Flask, Postgres (hosted free on [Neon](https://neon.tech)), deployed free on [Render](https://render.com).
There's no login: anyone with the URL can use it.

## Running locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python dev_server.py
```

Open http://localhost:5000. This starts a throwaway local
Postgres in `.pgdata/`, so you don't need Neon locally. Delete that folder to start fresh.

Tests:

```powershell
.venv\Scripts\python -m pytest
```

## Deploying

1. **Neon**: create a free project, then copy its connection string
   (`postgresql://...neon.tech/neondb?sslmode=require`).
2. **Render**: New → Blueprint → pick this repo. It reads `render.yaml`. Then set
   `DATABASE_URL` to the Neon connection string (service → Environment).
   Tables are created on first start.

The free Render service sleeps after ~15 minutes idle, so the first load after
that takes 30 to 60 seconds.

## Database

See [schema.sql](schema.sql). Current counts live in `stock` (recipe × location);
every change is logged in `movements`, and `batches` records each time a recipe is made.

## Morning report

Each morning the app pushes a summary to the [ntfy](https://ntfy.sh) phone app:
meals left per category, plus MAKE MORE (1 left; urgent at 0) and THAW
(1 or fewer in the fridge with some in the freezer). The same list shows on
the Stock page, and `/report.txt` has the plain-text version.

Setup:
1. Install ntfy on each phone and subscribe to your topic name (treat it like a password).
2. In Render, set `NTFY_TOPIC` to that topic name. Then use **Send to phone** on the Stock page to test.
   Also set `NTFY_TOKEN` to an access token from a free ntfy.sh account (Account → Access tokens).
   ntfy.sh rate-limits anonymous senders per IP, and Render's free tier shares its IPs with
   other apps, so without a token you can hit "429 Too Many Requests".
3. In GitHub, add a repository secret `APP_URL` with the Render site URL.
   `.github/workflows/morning-report.yml` then triggers the send daily around 7 AM Eastern
   (`REPORT_HOUR`). GitHub pauses scheduled workflows after 60 days without repo
   activity; re-enable it from the Actions tab if that happens.
