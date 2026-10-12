-- Safe to run on every startup: everything is IF NOT EXISTS / ON CONFLICT DO NOTHING.

CREATE TABLE IF NOT EXISTS locations (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    short_name  TEXT NOT NULL,
    sort_order  INT  NOT NULL DEFAULT 0
);

INSERT INTO locations (name, short_name, sort_order) VALUES
    ('Freezer',              'Freezer',  1),
    ('Fridge (thawing)',     'Thawing',  2),
    ('Fridge (in syringes)', 'Syringes', 3)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS recipes (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    ingredients   TEXT NOT NULL DEFAULT '',
    archived      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Removed fields (for databases created before they were dropped).
ALTER TABLE recipes DROP COLUMN IF EXISTS instructions;
ALTER TABLE recipes DROP COLUMN IF EXISTS notes;

-- One row each time a recipe is made.
CREATE TABLE IF NOT EXISTS batches (
    id          SERIAL PRIMARY KEY,
    recipe_id   INT  NOT NULL REFERENCES recipes(id),
    made_on     DATE NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current number of meals of each recipe in each location.
CREATE TABLE IF NOT EXISTS stock (
    recipe_id    INT NOT NULL REFERENCES recipes(id),
    location_id  INT NOT NULL REFERENCES locations(id),
    quantity     INT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (recipe_id, location_id)
);

-- Every change to stock, for history, undo, and (later) usage-rate reporting.
--   made:      to_location only (batch_id set)
--   moved:     from_location -> to_location
--   used:      from_location only
--   discarded: from_location only
CREATE TABLE IF NOT EXISTS movements (
    id                SERIAL PRIMARY KEY,
    recipe_id         INT  NOT NULL REFERENCES recipes(id),
    batch_id          INT  REFERENCES batches(id),
    kind              TEXT NOT NULL CHECK (kind IN ('made', 'moved', 'used', 'discarded')),
    from_location_id  INT  REFERENCES locations(id),
    to_location_id    INT  REFERENCES locations(id),
    quantity          INT  NOT NULL CHECK (quantity > 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS movements_created_at_idx ON movements (created_at DESC);
CREATE INDEX IF NOT EXISTS batches_recipe_idx ON batches (recipe_id, made_on DESC);
