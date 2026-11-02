"""Generate the macpurge icons.

Outputs:
    resources/menubar_icon.png      22x22 template (1x), monochrome black-on-transparent
    resources/menubar_icon@2x.png   44x44 template (2x)
    resources/icon.icns             full-color app icon (16..1024)

Run:
    pip install -e ".[build]"
    python tools/make_icons.py

Design:
    A clean 4-point sparkle / starburst — universal "clean / shine / fresh"
    symbolism. Geometric, monoline, in the same minimalist family as
    Anthropic's radial mark. Vertical points are slightly longer than
    horizontal for a more elegant feel.

The .icns step uses macOS's iconutil (preinstalled). Needs Pillow.
"""

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "resources"
RESOURCES.mkdir(exist_ok=True)


def _sparkle_petal(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                   length: float, width: float, angle_deg: float,
                   fill) -> None:
    """Draw one diamond-shaped petal of the sparkle, rotated.

    Petal anchor is at center (0,0); tip at (length, 0); widest at
    ~40% of length; the "base" pinches narrow at the center so adjacent
    petals visually merge into a four-point star.
    """
    a = math.radians(angle_deg)
    base_pinch = width * 0.25  # narrow at center for crisp star shape
    pts = [
        (length, 0),
        (length * 0.42, width),
        (0, base_pinch),
        (length * 0.42, -width),
    ]
    rotated = [
        (cx + p[0] * math.cos(a) - p[1] * math.sin(a),
         cy + p[0] * math.sin(a) + p[1] * math.cos(a))
        for p in pts
    ]
    draw.polygon(rotated, fill=fill)


def _draw_sparkle(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                  vertical_len: float, horizontal_len: float,
                  petal_width: float, fill) -> None:
    """Render the 4-point sparkle around (cx, cy)."""
    _sparkle_petal(draw, cx, cy, vertical_len, petal_width, -90, fill)
    _sparkle_petal(draw, cx, cy, vertical_len, petal_width, 90, fill)
    _sparkle_petal(draw, cx, cy, horizontal_len, petal_width, 0, fill)
    _sparkle_petal(draw, cx, cy, horizontal_len, petal_width, 180, fill)


def make_menubar_icon() -> None:
    """Template image for NSStatusItem. macOS auto-tints for dark/light mode."""
    for px, suffix in [(22, ""), (44, "@2x")]:
        s = px * 8  # supersample heavily for crisp downsample
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        _draw_sparkle(
            draw,
            cx=s / 2, cy=s / 2,
            vertical_len=s * 0.46,
            horizontal_len=s * 0.36,
            petal_width=s * 0.085,
            fill=(0, 0, 0, 255),
        )
        img = img.resize((px, px), Image.LANCZOS)
        out = RESOURCES / f"menubar_icon{suffix}.png"
        img.save(out, "PNG")
        print(f"  wrote {out.relative_to(ROOT)}")


def make_menubar_alert_icon() -> None:
    """Same sparkle plus a dot top-right: shown when memory pressure is high.
    Template images are monochrome, so the signal is shape, not colour."""
    for px, suffix in [(22, ""), (44, "@2x")]:
        s = px * 8
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        _draw_sparkle(draw, cx=s * 0.44, cy=s * 0.54, vertical_len=s * 0.42,
                      horizontal_len=s * 0.33, petal_width=s * 0.08,
                      fill=(0, 0, 0, 255))
        r, cx, cy = s * 0.15, s * 0.82, s * 0.18
        # Clear a ring around the dot so it reads as separate from the star.
        ring = r + s * 0.06
        draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), fill=(0, 0, 0, 0))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 255))
        img = img.resize((px, px), Image.LANCZOS)
        out = RESOURCES / f"menubar_icon_alert{suffix}.png"
        img.save(out, "PNG")
        print(f"  wrote {out.relative_to(ROOT)}")


def _color_app_icon(size: int) -> Image.Image:
    """Full-color sparkle on a soft rounded backdrop."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded-square backdrop (typical macOS app-icon shape).
    pad = int(s * 0.08)
    radius = int(s * 0.22)
    outer = (pad, pad, s - pad, s - pad)
    # Soft warm-white background — neutral, lets the mark do the work.
    draw.rounded_rectangle(outer, radius=radius, fill=(248, 248, 250, 255))

    # Subtle inner highlight (very faint cool ring along top edge).
    highlight = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.rounded_rectangle(
        (pad, pad, s - pad, pad + int(s * 0.20)),
        radius=radius, fill=(255, 255, 255, 60),
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(radius=int(s * 0.02)))
    img = Image.alpha_composite(img, highlight)
    draw = ImageDraw.Draw(img)

    # The sparkle, in a deep teal — fresh / clean color, distinct from
    # Anthropic orange so the brand identities don't collide.
    teal = (40, 130, 145, 255)
    teal_dark = (20, 90, 105, 255)

    # Soft drop shadow under the mark.
    shadow_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    _draw_sparkle(
        sd,
        cx=s / 2 + int(s * 0.005),
        cy=s / 2 + int(s * 0.012),
        vertical_len=s * 0.36,
        horizontal_len=s * 0.28,
        petal_width=s * 0.07,
        fill=(0, 0, 0, 90),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=int(s * 0.012)))
    img = Image.alpha_composite(img, shadow_layer)
    draw = ImageDraw.Draw(img)

    # Outer (darker) sparkle — gives the mark slight depth.
    _draw_sparkle(
        draw,
        cx=s / 2, cy=s / 2,
        vertical_len=s * 0.36,
        horizontal_len=s * 0.28,
        petal_width=s * 0.073,
        fill=teal_dark,
    )
    # Inner (brighter) sparkle, slightly smaller — the "shine".
    _draw_sparkle(
        draw,
        cx=s / 2, cy=s / 2,
        vertical_len=s * 0.33,
        horizontal_len=s * 0.255,
        petal_width=s * 0.058,
        fill=teal,
    )

    return img.resize((size, size), Image.LANCZOS)


def make_app_icon() -> None:
    """Build .icns from a generated 1024 master via iconutil."""
    iconset = RESOURCES / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    sizes = [
        (16, ""), (16, "@2x"),
        (32, ""), (32, "@2x"),
        (128, ""), (128, "@2x"),
        (256, ""), (256, "@2x"),
        (512, ""), (512, "@2x"),
    ]
    master = _color_app_icon(1024)
    for px, suffix in sizes:
        actual = px * (2 if suffix == "@2x" else 1)
        resized = master.resize((actual, actual), Image.LANCZOS)
        out = iconset / f"icon_{px}x{px}{suffix}.png"
        resized.save(out, "PNG")

    icns = RESOURCES / "icon.icns"
    if icns.exists():
        icns.unlink()
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("iconutil failed:", result.stderr, file=sys.stderr)
        sys.exit(1)
    shutil.rmtree(iconset)
    print(f"  wrote {icns.relative_to(ROOT)}")


if __name__ == "__main__":
    print("Generating menu bar icon…")
    make_menubar_icon()
    make_menubar_alert_icon()
    print("Generating app icon (.icns)…")
    make_app_icon()
    print("Done.")
