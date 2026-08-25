"""Generate the concept D app icons.

Kept as a script so the icons can be regenerated rather than being opaque
binaries. Mirrors icon.svg: pine rounded square, cream roof, amber warmth.
"""
import math
from PIL import Image, ImageDraw

PINE = (47, 93, 80, 255)
CREAM = (246, 242, 234, 255)
AMBER = (224, 138, 46, 255)

S = 2048          # supersampled canvas, downscaled for clean edges
SCALE = S / 512.0


def p(*xy):
    return [(x * SCALE, y * SCALE) for x, y in xy]


def wave_points():
    """The amber stroke from icon.svg, flattened into dense line segments."""
    def bez(p0, p1, p2, t):
        return (
            (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1],
        )

    pts = []
    seg1 = [(256, 258), (292, 292), (256, 326)]
    seg2 = [(256, 326), (220, 360), (256, 394)]
    for seg in (seg1, seg2):
        for i in range(201):
            pts.append(bez(seg[0], seg[1], seg[2], i / 200))
    return pts


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_art(img, inset=0.0):
    """Draw roof + warmth. `inset` shrinks the art for maskable safe area."""
    d = ImageDraw.Draw(img)
    cx = cy = 256.0

    def tx(x, y):
        return (cx + (x - cx) * (1 - inset), cy + (y - cy) * (1 - inset))

    roof = [(256, 132), (404, 268), (360, 268), (256, 182), (152, 268), (108, 268)]
    d.polygon(p(*[tx(*q) for q in roof]), fill=CREAM)

    # Stamping a round brush along the path gives a genuinely round stroke.
    # PIL's polyline joints leave notches on tight curves.
    r = (30 * (1 - inset) * SCALE) / 2
    for q in wave_points():
        x, y = tx(*q)
        x *= SCALE
        y *= SCALE
        d.ellipse([x - r, y - r, x + r, y + r], fill=AMBER)


def build(path, size, maskable=False, full_bleed=False):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        # A maskable icon is cropped to a circle by the platform, so the
        # background must bleed to the edges and the art must sit inside.
        d.rectangle([0, 0, S, S], fill=PINE)
        draw_art(img, inset=0.20)
    elif full_bleed:
        # iOS applies its own corner radius to apple-touch-icon. Supplying
        # pre-rounded corners would leave dark notches around the edge.
        d.rectangle([0, 0, S, S], fill=PINE)
        draw_art(img)
    else:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(112 * SCALE), fill=PINE)
        draw_art(img)
    img.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
    print("wrote", path)


if __name__ == "__main__":
    base = "app/static/concepts/d/"
    build(base + "icon-192.png", 192)
    build(base + "icon-512.png", 512)
    build(base + "icon-180.png", 180, full_bleed=True)   # apple-touch-icon
    build(base + "icon-maskable.png", 512, maskable=True)
