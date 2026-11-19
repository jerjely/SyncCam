"""Draws the SyncCam logo and writes assets/icon_512.png, icon_256.png, SyncCam.ico, SyncCam.icns.

    pip install pillow numpy
    python tools/make_icons.py

Style: rounded navy tile (#101d3d at the top to #060b1a at the bottom) with one
bright symbol, here a camera lens over a TTL pulse, with the IR LED as an amber dot.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
TOP, BOTTOM = (0x10, 0x1D, 0x3D), (0x06, 0x0B, 0x1A)
TEAL, TEAL_CORE = (18, 164, 184), (200, 246, 252)
AMBER = (255, 170, 40)


def render(size: int = 1024) -> Image.Image:
    ss = 2
    n = size * ss
    ramp = np.linspace(0.0, 1.0, n)[:, None, None]
    grad = (np.array(TOP) * (1 - ramp) + np.array(BOTTOM) * ramp).astype(np.uint8)
    grad = np.repeat(grad, n, axis=1)
    tile = Image.fromarray(grad, "RGB").convert("RGBA")
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.225), fill=255)
    tile.putalpha(mask)
    edge = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((n * 0.004,) * 2 + (n * 0.996,) * 2, radius=int(n * 0.222),
                                           outline=(255, 255, 255, 26), width=max(2, int(n * 0.004)))
    tile = Image.alpha_composite(tile, edge)

    def layer():
        return Image.new("RGBA", (n, n), (0, 0, 0, 0))

    def glowify(img, blur, alpha):
        g = img.filter(ImageFilter.GaussianBlur(blur * n))
        g.putalpha(g.getchannel("A").point(lambda v: int(v * alpha)))
        return g

    # lens: two rings and a pupil
    cx, cy = 0.5 * n, 0.42 * n
    lens = layer()
    d = ImageDraw.Draw(lens)
    for r, w in ((0.25, 0.034), (0.15, 0.022)):
        R = r * n
        d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=TEAL + (255,), width=int(w * n))
    R = 0.065 * n
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=TEAL + (255,))
    core = layer()
    dc = ImageDraw.Draw(core)
    for r, w in ((0.25, 0.012), (0.15, 0.008)):
        R = r * n
        dc.ellipse((cx - R, cy - R, cx + R, cy + R), outline=TEAL_CORE + (255,), width=int(w * n))
    R = 0.03 * n
    dc.ellipse((cx - R, cy - R, cx + R, cy + R), fill=TEAL_CORE + (255,))

    # TTL pulse line under the lens
    lo, hi = 0.84, 0.74
    pts = [(0.14, lo), (0.40, lo), (0.40, hi), (0.60, hi), (0.60, lo), (0.86, lo)]
    pts = [(x * n, y * n) for x, y in pts]
    pulse = layer()
    dp = ImageDraw.Draw(pulse)
    dp.line(pts, fill=(235, 240, 255, 255), width=int(0.03 * n), joint="curve")
    for px, py in (pts[0], pts[-1]):
        r = 0.015 * n
        dp.ellipse((px - r, py - r, px + r, py + r), fill=(235, 240, 255, 255))

    # IR LED dot
    led = layer()
    lx, ly, lr = 0.79 * n, 0.19 * n, 0.045 * n
    ImageDraw.Draw(led).ellipse((lx - lr, ly - lr, lx + lr, ly + lr), fill=AMBER + (255,))
    led_core = layer()
    lr2 = 0.02 * n
    ImageDraw.Draw(led_core).ellipse((lx - lr2, ly - lr2, lx + lr2, ly + lr2), fill=(255, 240, 200, 255))

    art = layer()
    for img in (glowify(lens, 0.03, 0.9), lens, core, glowify(pulse, 0.02, 0.5), pulse,
                glowify(led, 0.035, 1.0), led, led_core):
        art = Image.alpha_composite(art, img)
    art.putalpha(ImageChops.multiply(art.getchannel("A"), mask))
    tile = Image.alpha_composite(tile, art)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = render(1024)
    big512 = big.resize((512, 512), Image.Resampling.LANCZOS)
    big512.save(OUT / "icon_512.png", optimize=True)
    big.resize((256, 256), Image.Resampling.LANCZOS).save(OUT / "icon_256.png", optimize=True)
    big512.save(OUT / "SyncCam.ico",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(OUT / "SyncCam.icns")
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
