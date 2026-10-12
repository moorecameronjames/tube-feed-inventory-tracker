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
