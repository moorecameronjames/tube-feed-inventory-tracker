-- Safe to run on every startup: everything is IF NOT EXISTS / ON CONFLICT DO NOTHING,
-- and one-time migrations check whether they've already run.

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

-- Which meal of the day a batch is for. Chosen per batch, not per recipe,
-- so the same recipe can be lunch one week and dinner the next.
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INT  NOT NULL DEFAULT 0
);

INSERT INTO categories (name, sort_order) VALUES
    ('Breakfast', 1),
    ('Lunch',     2),
    ('Snack',     3),
    ('Dinner',    4)
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
    id           SERIAL PRIMARY KEY,
    recipe_id    INT  NOT NULL REFERENCES recipes(id),
    category_id  INT  NOT NULL REFERENCES categories(id),
    made_on      DATE NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current number of syringes of each recipe + category in each location.
CREATE TABLE IF NOT EXISTS stock (
    recipe_id    INT NOT NULL REFERENCES recipes(id),
    category_id  INT NOT NULL REFERENCES categories(id),
    location_id  INT NOT NULL REFERENCES locations(id),
    quantity     INT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (recipe_id, category_id, location_id)
);

-- Every change to stock, for history, undo, and the daily report.
--   made:      to_location only (batch_id set)
--   moved:     from_location -> to_location
--   used:      from_location only
--   discarded: from_location only
CREATE TABLE IF NOT EXISTS movements (
    id                SERIAL PRIMARY KEY,
    recipe_id         INT  NOT NULL REFERENCES recipes(id),
    category_id       INT  NOT NULL REFERENCES categories(id),
    batch_id          INT  REFERENCES batches(id),
    kind              TEXT NOT NULL CHECK (kind IN ('made', 'moved', 'used', 'discarded')),
    from_location_id  INT  REFERENCES locations(id),
    to_location_id    INT  REFERENCES locations(id),
    quantity          INT  NOT NULL CHECK (quantity > 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration: databases created before categories existed. Everything already
-- logged back then was snacks.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'stock'
                     AND column_name = 'category_id') THEN
        ALTER TABLE batches   ADD COLUMN category_id INT REFERENCES categories(id);
        ALTER TABLE stock     ADD COLUMN category_id INT REFERENCES categories(id);
        ALTER TABLE movements ADD COLUMN category_id INT REFERENCES categories(id);

        UPDATE batches   SET category_id = (SELECT id FROM categories WHERE name = 'Snack');
        UPDATE stock     SET category_id = (SELECT id FROM categories WHERE name = 'Snack');
        UPDATE movements SET category_id = (SELECT id FROM categories WHERE name = 'Snack');

        ALTER TABLE batches   ALTER COLUMN category_id SET NOT NULL;
        ALTER TABLE stock     ALTER COLUMN category_id SET NOT NULL;
        ALTER TABLE movements ALTER COLUMN category_id SET NOT NULL;

        ALTER TABLE stock DROP CONSTRAINT stock_pkey;
        ALTER TABLE stock ADD PRIMARY KEY (recipe_id, category_id, location_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS movements_created_at_idx ON movements (created_at DESC);
CREATE INDEX IF NOT EXISTS batches_recipe_idx ON batches (recipe_id, made_on DESC);

-- Small key/value store for app bookkeeping (e.g. the date the morning report last went out).
CREATE TABLE IF NOT EXISTS app_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
