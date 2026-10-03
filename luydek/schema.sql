-- LuyDek schema. Timestamps are naive shop-local "YYYY-MM-DD HH:MM:SS".
-- Stock is always counted in a product's base unit (can, box, sachet), so a
-- case, a 6-pack or a broken strip is just a different number of base units.

CREATE TABLE IF NOT EXISTS suppliers (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL UNIQUE,
    emoji            TEXT    NOT NULL DEFAULT '🚚',
    visit_weekday    INTEGER NOT NULL,            -- 0 = Monday ... 6 = Sunday
    visit_every_days INTEGER NOT NULL DEFAULT 7,
    lead_days        INTEGER NOT NULL DEFAULT 0,  -- visit -> goods on the shelf
    min_order        REAL    NOT NULL DEFAULT 0,  -- dollars
    contact          TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    sku             TEXT    NOT NULL UNIQUE,
    name_en         TEXT    NOT NULL,
    name_km         TEXT    NOT NULL,
    unit            TEXT    NOT NULL DEFAULT 'piece',
    case_size       INTEGER NOT NULL DEFAULT 1,     -- base units per supplier case
    supplier_id     INTEGER REFERENCES suppliers(id),
    is_critical     INTEGER NOT NULL DEFAULT 0,     -- on the store's must-never-run-out list
    shelf_life_days INTEGER,
    unit_cost       REAL    NOT NULL DEFAULT 0,     -- dollars per base unit, latest invoice
    active          INTEGER NOT NULL DEFAULT 1,
    sold_loose      INTEGER NOT NULL DEFAULT 0,     -- medicine sold by the tablet: the base unit is one tablet
    origin          TEXT    NOT NULL DEFAULT 'catalog', -- catalog / counter (named at a sale) / invoice
    added_at        TEXT,
    location_id     INTEGER REFERENCES locations(id)    -- where it lives in the shop
);

-- The places in the shop (Fridge, Shelf A, Back room, Drawer 4): where each product lives and
-- where each delivery was put, so the shelf check walks the shop in order.
CREATE TABLE IF NOT EXISTS locations (
    id     INTEGER PRIMARY KEY,
    name   TEXT    NOT NULL UNIQUE,
    sort   INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- What the camera sees. Every product has a single-unit pack; a shrink-wrapped
-- 6-pack is its own pack with units = 6, enrolled with its own photos.
CREATE TABLE IF NOT EXISTS product_packs (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    label      TEXT    NOT NULL,
    units      INTEGER NOT NULL DEFAULT 1,
    barcode    TEXT UNIQUE
);

-- "Coca 330" on an invoice, "Coke" from the owner, the catalog name: one product.
CREATE TABLE IF NOT EXISTS product_aliases (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    alias_norm TEXT    NOT NULL UNIQUE,
    source     TEXT    NOT NULL                      -- catalog / invoice / owner / distributor
);

-- The store's visual catalog. Adding a product means adding rows here, not
-- retraining anything. Ignore classes (phone, cash, bag) use ignore_label.
CREATE TABLE IF NOT EXISTS gallery_images (
    id           INTEGER PRIMARY KEY,
    pack_id      INTEGER REFERENCES product_packs(id),
    ignore_label TEXT,
    crop_path    TEXT    NOT NULL,
    embedding    BLOB    NOT NULL,                   -- float32, L2-normalised
    source       TEXT    NOT NULL,                   -- enrollment / correction
    created_at   TEXT    NOT NULL,
    area_frac    REAL,                               -- its size on the calibrated mat (share of the frame)
    CHECK ((pack_id IS NULL) <> (ignore_label IS NULL))
);

CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    closed_at  TEXT,
    photo_path TEXT,                                 -- last still frame; deleted once settled
    source     TEXT NOT NULL DEFAULT 'camera',       -- camera / upload
    status     TEXT NOT NULL DEFAULT 'open'          -- open / auto / pending / verified / best_guess
);

-- One row per item detected in each still frame of an open transaction.
-- Closing the transaction turns these into transaction_items.
CREATE TABLE IF NOT EXISTS frame_detections (
    id              INTEGER PRIMARY KEY,
    txn_id          INTEGER NOT NULL REFERENCES transactions(id),
    frame_no        INTEGER NOT NULL,
    pack_id         INTEGER REFERENCES product_packs(id),
    confidence      REAL    NOT NULL,
    tier            TEXT    NOT NULL,
    method          TEXT    NOT NULL,
    bbox            TEXT    NOT NULL,
    crop_path       TEXT,
    candidates_json TEXT    NOT NULL DEFAULT '[]',
    hint            TEXT
);

CREATE TABLE IF NOT EXISTS transaction_items (
    id              INTEGER PRIMARY KEY,
    txn_id          INTEGER NOT NULL REFERENCES transactions(id),
    pack_id         INTEGER REFERENCES product_packs(id),  -- current best guess
    guess_pack_id   INTEGER REFERENCES product_packs(id),  -- what the camera first said
    count           INTEGER NOT NULL DEFAULT 1,            -- packs; units = count * pack.units
    confidence      REAL    NOT NULL,
    tier            TEXT    NOT NULL,                      -- high / medium / low
    method          TEXT    NOT NULL,                      -- barcode / image / multipack
    bbox            TEXT,
    crop_path       TEXT,
    candidates_json TEXT    NOT NULL DEFAULT '[]',
    status          TEXT    NOT NULL,   -- auto / pending / confirmed / corrected / best_guess / removed / unknown / split (several items in one photo)
    verified_by     TEXT,
    verified_at     TEXT,
    digest_id       INTEGER,
    label_text      TEXT,          -- what the label reader saw printed on a look-alike
    label_checked_at TEXT,         -- set once the label reader has looked (read or not)
    tray_json       TEXT,          -- tablets from the dispensing tray: piece sizes and what was sure
    name_guess      TEXT,          -- a new product's name as read off the package, for the owner to accept
    name_guess_km   TEXT,
    name_checked_at TEXT,          -- set once the package has been read for a name (read or not)
    hint            TEXT           -- why the camera asked: junk (looked like clutter) / background (like the mat)
);

-- What the camera saw and did NOT record: junk it was sure about (a phone, a hand), or a change
-- in the background (a shifted paper, a patch of light). Kept a while so the owner can see it in
-- the camera log and say "that was a sale" if it was.
CREATE TABLE IF NOT EXISTS ignored_detections (
    id            INTEGER PRIMARY KEY,
    txn_id        INTEGER,                         -- the sale it happened in (may be gone if nothing was sold)
    reason        TEXT    NOT NULL,                -- an ignore class (phone, cash, hand, other) or 'background'
    score         REAL    NOT NULL,                -- how sure: the ignore class score, or the background likeness
    best_pack_id  INTEGER REFERENCES product_packs(id),  -- the product it looked most like
    best_score    REAL,
    bbox          TEXT,
    crop_path     TEXT,
    created_at    TEXT    NOT NULL,
    fixed_item_id INTEGER                          -- set when the owner said it was a sale after all
);
CREATE INDEX IF NOT EXISTS ignored_detections_at ON ignored_detections (created_at);

-- What lay on the dispensing tray in each still frame of an open transaction.
CREATE TABLE IF NOT EXISTS tray_readings (
    id         INTEGER PRIMARY KEY,
    txn_id     INTEGER NOT NULL REFERENCES transactions(id),
    frame_no   INTEGER NOT NULL,
    pieces_json TEXT   NOT NULL,                     -- {"areas": [share of the frame, ...], "oversized": bool}
    crop_path  TEXT
);

-- How big one tablet of a loose-sold medicine looks on the tray, learned from checked counts.
CREATE TABLE IF NOT EXISTS tray_units (
    product_id   INTEGER PRIMARY KEY REFERENCES products(id),
    samples_json TEXT NOT NULL,                      -- recent one-tablet areas, newest last
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    qty         INTEGER NOT NULL,                  -- signed base units
    type        TEXT    NOT NULL,                  -- opening / sale / delivery / freebie / adjustment / spot_check
    ref_type    TEXT,                              -- transaction_item / invoice_line / adjust / sim ...
    ref_id      INTEGER,
    reason      TEXT,
    unit_cost   REAL,
    expiry_date TEXT,
    simulated   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    -- A movement that brings stock in starts a batch (its own id); a removal of one batch (expired
    -- stock taken off the shelf) names it here. Sales name none: they come from the batch that
    -- expires first. Where the batch was put, and the lot printed on it.
    batch_id    INTEGER,
    location_id INTEGER REFERENCES locations(id),
    lot_no      TEXT
);
CREATE INDEX IF NOT EXISTS ix_moves_product_time ON stock_movements(product_id, created_at);
CREATE INDEX IF NOT EXISTS ix_moves_type_time ON stock_movements(type, created_at);

CREATE TABLE IF NOT EXISTS invoices (
    id                INTEGER PRIMARY KEY,
    supplier_id       INTEGER REFERENCES suppliers(id),
    supplier_name_raw TEXT,
    photo_path        TEXT,
    status            TEXT NOT NULL,  -- queued / extracted / confirmed / rejected / failed
    source            TEXT NOT NULL DEFAULT 'photo',   -- photo / manual / distributor
    extracted_json    TEXT,
    error             TEXT,
    chat_id           INTEGER,         -- Telegram chat to answer when a queued read completes
    received_at       TEXT NOT NULL,
    confirmed_at      TEXT
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id              INTEGER PRIMARY KEY,
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id),
    product_id      INTEGER REFERENCES products(id),
    text_as_written TEXT    NOT NULL,
    qty             REAL    NOT NULL,
    unit            TEXT    NOT NULL,              -- case / pack / piece
    units_total     INTEGER,
    unit_cost       REAL,                          -- per invoice unit (per case if unit = case)
    expiry_date     TEXT,
    is_free         INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    matched_by      TEXT,                          -- alias / model / owner / manual / new
    -- For lines that aren't a product yet: what the reader thinks it is, to add it in one tap.
    name_suggested    TEXT,                        -- a clean shelf name: brand, product, variant, size
    name_suggested_km TEXT,
    units_per_case    INTEGER,                     -- only when printed on the invoice
    unit_name         TEXT,                        -- what one single item is: can, bottle, sachet, box...
    lot_no            TEXT                         -- batch or lot number, when printed
);

CREATE TABLE IF NOT EXISTS forecasts (
    id                INTEGER PRIMARY KEY,
    visit_date        TEXT    NOT NULL,
    supplier_id       INTEGER NOT NULL REFERENCES suppliers(id),
    product_id        INTEGER NOT NULL REFERENCES products(id),
    avg_daily_sales   REAL    NOT NULL,
    current_stock     INTEGER NOT NULL,
    recommended_units INTEGER NOT NULL,
    recommended_cases INTEGER NOT NULL,
    action            TEXT    NOT NULL,             -- order / skip / sleeping / market / check
    reason            TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    followed          INTEGER,                      -- NULL until the next delivery settles it
    UNIQUE (visit_date, product_id)
);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY,
    sent_at    TEXT NOT NULL,
    item_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_users (
    chat_id    INTEGER PRIMARY KEY,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'verifier',
    lang       TEXT NOT NULL DEFAULT 'km',
    created_at TEXT NOT NULL
);

-- Telegram messages that carry buttons for something, so that settling it
-- anywhere (another family member's phone, the dashboard) updates every copy.
CREATE TABLE IF NOT EXISTS bot_messages (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,       -- item / grid / invoice
    ref_id     INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bot_messages_ref ON bot_messages(kind, ref_id);

-- Every button press. Taps per day is the verification-fatigue health metric.
CREATE TABLE IF NOT EXISTS taps (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Every count of a shelf, the ones that matched too: quick daily counts keep the numbers honest.
CREATE TABLE IF NOT EXISTS stock_counts (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    counted    INTEGER NOT NULL,
    expected   INTEGER NOT NULL,
    source     TEXT    NOT NULL,   -- spot_check / product_page / count_page / stock_in
    counted_by TEXT,
    created_at TEXT    NOT NULL
);

-- Out-of-stock and running-low alerts already sent, so each goes out once until the product recovers.
CREATE TABLE IF NOT EXISTS stock_alerts (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    state      TEXT NOT NULL,      -- out / low
    sent_at    TEXT NOT NULL
);
