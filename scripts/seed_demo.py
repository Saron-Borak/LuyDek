"""Create the database and load the demo catalog (suppliers, products, packs, aliases).

    python scripts/seed_demo.py            # create tables, load the seed if the catalog is empty
    python scripts/seed_demo.py --reset    # wipe every row first (catalog, sales, gallery, bot users)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek import db  # noqa: E402
from luydek.config import load_config  # noqa: E402
from luydek.services import catalog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete all existing rows first")
    parser.add_argument("--seed-file", type=Path, help="catalog YAML (default: paths.seed_catalog in config)")
    args = parser.parse_args()

    cfg = load_config()
    seed_file = args.seed_file or cfg.resolve(cfg.paths.seed_catalog)
    with db.session(cfg) as conn:
        db.init_schema(conn)
        if args.reset:
            catalog.reset(conn)
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            print("Catalog already loaded; use --reset to reload it.")
            return
        skus = catalog.load_seed(conn, seed_file)
        n_sup = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
        n_packs = conn.execute("SELECT COUNT(*) FROM product_packs").fetchone()[0]
    print(f"Loaded {len(skus)} products ({n_packs} packs) from {n_sup} suppliers into {cfg.db_path}")


if __name__ == "__main__":
    main()
