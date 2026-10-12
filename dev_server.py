"""Run the app locally against a throwaway Postgres (no Neon needed).

    python dev_server.py

Data is kept in .pgdata/ between runs. Log in with password "dev"
unless APP_PASSWORD is set.
"""

import os
from pathlib import Path

import pgserver

from app import create_app

if __name__ == "__main__":
    server = pgserver.get_server(Path(__file__).parent / ".pgdata", cleanup_mode="stop")
    os.environ.setdefault("APP_PASSWORD", "dev")
    os.environ.setdefault("SECRET_KEY", "dev-only-secret")
    os.environ["DATABASE_URL"] = server.get_uri()
    create_app().run(debug=True, port=int(os.environ.get("PORT", 5000)), use_reloader=False)
