"""Send photos to the running server as sales, one sale per photo (a CLI demo backup).

    python scripts/sale_from_photo.py counter1.jpg counter2.jpg
    python scripts/sale_from_photo.py --server http://192.168.1.20:8000 --use-reference shot.jpg
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("photos", nargs="+", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--use-reference", action="store_true",
                        help="the photo was taken by the calibrated counter camera")
    args = parser.parse_args()

    with httpx.Client(base_url=args.server, timeout=120) as client:
        for photo in args.photos:
            r = client.post("/api/sale-photo", files={"image": (photo.name, photo.read_bytes(), "image/jpeg")},
                            data={"use_reference": str(args.use_reference).lower()})
            r.raise_for_status()
            s = r.json()["summary"]
            print(f"{photo.name}: sale #{s['txn_id']} - {s['auto']} auto, {s['pending']} to check")
            for line in s.get("lines", []):
                print(f"    {line['status']:<9} {line['tier']:<6} {line['score']:.3f}  {line['label']}")


if __name__ == "__main__":
    main()
