"""Builds the app's icons and arrow images from the pixel grids below.

Run:  python3 tools/make_art.py   (needs Pillow)
Writes src/appmeta/icon_front_8x8.png, icon_back_11x11.png, src/images/down.png
and up.png, plus a scaled-up preview in tools/preview.png.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "src" / "appmeta"
IMAGES = ROOT / "src" / "images"

PALETTE = {
    "u": (140, 255, 170), "U": (46, 204, 113), "V": (20, 130, 60),    # download green
    "p": (170, 215, 245), "t": (93, 173, 226), "T": (45, 115, 175),   # upload blue
    "n": (136, 136, 136),                                             # back screen grey
}

# Menu icon: a green arrow down and a blue arrow up
FRONT = [
    "......p.",
    ".u...ttt",
    ".U....t.",
    ".U....t.",
    ".U....t.",
    "UUU...T.",
    ".V......",
    "........",
]

BACK = [
    "...........",
    "..n.....n..",
    "..n....nnn.",
    "..n...nnnnn",
    "..n.....n..",
    "..n.....n..",
    "nnnnn...n..",
    ".nnn....n..",
    "..n.....n..",
    "...........",
    "...........",
]

# Arrows shown next to the two speeds, 5x7
DOWN = [
    "..u..",
    "..U..",
    "..U..",
    "..U..",
    "UUUUU",
    ".UUU.",
    "..V..",
]
UP = [
    "..p..",
    ".ttt.",
    "ttttt",
    "..t..",
    "..t..",
    "..t..",
    "..T..",
]


def to_image(rows):
    img = Image.new("RGBA", (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, r in enumerate(rows):
        assert len(r) == len(rows[0]), r
        for x, c in enumerate(r):
            if c != ".":
                img.putpixel((x, y), PALETTE[c] + (255,))
    return img


def main():
    META.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    made = {
        META / "icon_front_8x8.png": to_image(FRONT),
        META / "icon_back_11x11.png": to_image(BACK),
        IMAGES / "down.png": to_image(DOWN),
        IMAGES / "up.png": to_image(UP),
    }
    assert made[META / "icon_front_8x8.png"].size == (8, 8) and made[META / "icon_back_11x11.png"].size == (11, 11)
    sheet = Image.new("RGB", (16 * (8 + 11 + 5 + 5) + 80, 11 * 16 + 32), (12, 12, 12))
    x = 16
    for path, img in made.items():
        img.save(path)
        big = img.resize((img.width * 16, img.height * 16), Image.NEAREST)
        sheet.paste(big, (x, 16), big)
        x += big.width + 16
    sheet.save(ROOT / "tools" / "preview.png")
    print("icons and arrows written, preview in tools/preview.png")


if __name__ == "__main__":
    main()
