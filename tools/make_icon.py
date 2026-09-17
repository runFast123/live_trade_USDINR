"""Generate the application icon.

    python tools/make_icon.py

Draws an original mark in the Choice palette: the brand navy #0f1621 as the
field, the brand blue #2777f3 for the roll arc, and the brand gold #ffce02 for
the arrowhead. A circular arrow reads as "roll forward", which is what the app
does.

The mark is deliberately not Choice Broking's logo. Their wordmark is a
trademark, and shipping it on a third party application published to a public
repository would present this as Choice's own software. The palette carries the
family resemblance without claiming to be them.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

NAVY = (15, 22, 33, 255)        # #0f1621
BLUE = (39, 119, 243, 255)      # #2777f3
GOLD = (255, 206, 2, 255)       # #ffce02
EDGE = (39, 119, 243, 90)

SIZE = 1024                     # drawn large, then downsampled
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    return mask


def draw_mark() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Field
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * 0.22),
                           fill=NAVY)
    draw.rounded_rectangle((6, 6, SIZE - 7, SIZE - 7), radius=int(SIZE * 0.21),
                           outline=EDGE, width=6)

    # The roll arc: an open circle, gapped at the top right for the arrowhead.
    pad = int(SIZE * 0.26)
    box = (pad, pad, SIZE - pad, SIZE - pad)
    draw.arc(box, start=318, end=205, fill=BLUE, width=int(SIZE * 0.13))

    # Arrowhead closing the gap, pointing the way the position moves.
    cx = cy = SIZE / 2
    r = (SIZE - 2 * pad) / 2
    import math
    ang = math.radians(-46)
    tipx, tipy = cx + r * math.cos(ang), cy + r * math.sin(ang)
    h = SIZE * 0.145
    # Pointing up and to the right: the position moving out to the later month.
    draw.polygon([
        (tipx + h * 0.75, tipy - h * 0.75),
        (tipx - h * 0.55, tipy - h * 0.60),
        (tipx + h * 0.60, tipy + h * 0.55),
    ], fill=GOLD)

    img.putalpha(rounded_mask(SIZE, int(SIZE * 0.22)))
    return img


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)

    mark = draw_mark()
    png = os.path.join(assets, "icon.png")
    ico = os.path.join(assets, "icon.ico")

    mark.resize((256, 256), Image.LANCZOS).save(png)
    mark.save(ico, sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {png}")
    print(f"wrote {ico}  sizes {', '.join(str(s) for s in ICO_SIZES)}")


if __name__ == "__main__":
    main()
