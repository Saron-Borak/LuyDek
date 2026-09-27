"""Pick confidence thresholds from this store's own enrolled photos.

Leave-one-out: every gallery photo is matched against all the others, as if it
were a crop from a sale. That gives top-1 accuracy, which products get confused
with which, and, for each candidate (t_high, m_high), how many items would be
auto-accepted and how often those auto-accepts would be wrong.

    python scripts/calibrate.py                 # report + suggested config values
    python scripts/calibrate.py --precision 0.99

Enrollment photos are taken in one sitting, so these numbers are optimistic.
Re-run after a few days of owner corrections; those crops come from real sales.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek import db  # noqa: E402
from luydek.config import load_config  # noqa: E402
from luydek.recognition.matcher import Gallery  # noqa: E402
from luydek.services import catalog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--precision", type=float, default=0.98,
                        help="required share of auto-accepted items that are right")
    args = parser.parse_args()
    cfg = load_config()
    k = cfg.recognition.top_k_per_pack

    with db.session(cfg) as conn:
        gallery = Gallery.load(conn)
        names = {pk["pack_id"]: catalog.pack_label(pk, "en") for pk in catalog.packs(conn)}
    counts = Counter(gallery.labels)
    usable = [i for i, lab in enumerate(gallery.labels) if counts[lab] >= 2]
    if len(usable) < 4:
        sys.exit("Need at least 2 photos for several products. Enroll on the Catalog page first.")

    rows = []  # (correct, score, margin, true_label, predicted_label)
    for i in usable:
        keep = np.ones(len(gallery), bool)
        keep[i] = False
        others = Gallery(gallery.vectors[keep], [lab for j, lab in enumerate(gallery.labels) if keep[j]])
        m = others.match(gallery.vectors[i], k)
        predicted = ("pack", m.pack_id) if m.pack_id is not None else ("ignore", m.ignore_label)
        rows.append((predicted == gallery.labels[i], m.score, m.margin, gallery.labels[i], predicted))

    correct = np.array([r[0] for r in rows])
    score = np.array([r[1] for r in rows])
    margin = np.array([r[2] for r in rows])
    label = lambda lab: names.get(lab[1], str(lab[1])) if lab[0] == "pack" else f"[ignore] {lab[1]}"  # noqa: E731

    print(f"Gallery: {len(gallery)} photos, {len(counts)} classes; {len(usable)} tested leave-one-out")
    print(f"Top-1 accuracy: {correct.mean():.1%}")
    print(f"Scores: right {np.median(score[correct]):.3f} median"
          + (f", wrong {np.median(score[~correct]):.3f} median" if (~correct).any() else ""))

    confusions = Counter((label(r[3]), label(r[4])) for r in rows if not r[0])
    if confusions:
        print("\nMost confused (true -> predicted):")
        for (a, b), n in confusions.most_common(8):
            print(f"  {n:>3}  {a}  ->  {b}")

    print(f"\n{'t_high':>7} {'m_high':>7} {'auto':>6} {'auto precision':>15}")
    best = None
    for t_high in np.arange(0.70, 0.99, 0.01):
        for m_high in (0.0, 0.01, 0.02, 0.03, 0.05, 0.08):
            auto = (score >= t_high) & (margin >= m_high)
            if not auto.any():
                continue
            precision = correct[auto].mean()
            rate = auto.mean()
            # Most auto-accepts at the required precision; among ties, the
            # strictest score, because a product that was never enrolled can
            # still match something with a clear margin, but rarely a high score.
            if precision >= args.precision and (best is None or (rate, t_high) > (best[2], best[0])):
                best = (round(float(t_high), 2), m_high, rate, precision)
    for t_high in (0.80, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94):
        for m_high in (0.0, 0.03, 0.05):
            auto = (score >= t_high) & (margin >= m_high)
            if auto.any():
                print(f"{t_high:>7.2f} {m_high:>7.2f} {auto.mean():>6.0%} {correct[auto].mean():>15.1%}")

    t_low = float(np.percentile(score[correct], 5)) if correct.any() else cfg.recognition.t_low
    t_low = min(t_low, (best[0] if best else cfg.recognition.t_high) - 0.02)
    print("\nSuggested configs/config.yaml values:")
    if best:
        print(f"  t_high: {best[0]:.2f}\n  m_high: {best[1]:.2f}    "
              f"# {best[2]:.0%} auto-accepted at {best[3]:.1%} precision")
    else:
        print(f"  (no setting reaches {args.precision:.0%} precision: enroll more photos of the confused products)")
    print(f"  t_low: {t_low:.2f}     # below this the owner gets the photo grid instead of a yes/no")


if __name__ == "__main__":
    main()
