"""Draw LuyDek's mark and app icons into luydek/web/static/brand/.

The mark is a coin with a crescent moon on it: money that is asleep (លុយដេក).
Night-market palette: amber coin on deep violet.

    python scripts/make_brand.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "luydek" / "web" / "static" / "brand"
VIOLET, AMBER, AMBER_LIGHT = "#26215C", "#EF9F27", "#FAC775"

# On a 32-unit grid: coin, crescent (a violet disc with an amber disc taking a bite out of it), rim.
MARK = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="LuyDek">
  <circle cx="16" cy="16" r="14" fill="{AMBER}"/>
  <circle cx="16" cy="16" r="11.2" fill="none" stroke="{AMBER_LIGHT}" stroke-width="1.2"/>
  <circle cx="14.6" cy="16.6" r="7.4" fill="{VIOLET}"/>
  <circle cx="18.2" cy="13.8" r="6.4" fill="{AMBER}"/>
</svg>
"""


def icon(size: int, pad: float = 0.16, rounded: bool = True) -> Image.Image:
    """The mark on a violet tile, drawn at 4x and scaled down for smooth edges."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=VIOLET)
    else:
        d.rectangle((0, 0, s, s), fill=VIOLET)
    k = s * (1 - 2 * pad) / 32   # grid unit
    o = s * pad

    def disc(cx, cy, r, fill=None, outline=None, width=0):
        d.ellipse((o + (cx - r) * k, o + (cy - r) * k, o + (cx + r) * k, o + (cy + r) * k),
                  fill=fill, outline=outline, width=width)

    disc(16, 16, 14, AMBER)
    disc(16, 16, 11.2, outline=AMBER_LIGHT, width=max(1, int(1.2 * k)))
    disc(14.6, 16.6, 7.4, VIOLET)
    disc(18.2, 13.8, 6.4, AMBER)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logo.svg").write_text(MARK, encoding="utf-8")
    (OUT / "favicon.svg").write_text(MARK, encoding="utf-8")
    for size in (192, 512):
        icon(size).save(OUT / f"icon-{size}.png")
    icon(512, pad=0.24, rounded=False).save(OUT / "icon-maskable-512.png")  # Android crops these to a circle
    icon(180, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png")
    icon(64, pad=0.04).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
