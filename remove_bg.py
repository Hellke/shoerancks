"""Remove backgrounds from shoe images and save as standardised transparent PNGs."""
import os
from pathlib import Path
from PIL import Image
from rembg import remove

CANVAS = (600, 340)   # final canvas size (width x height)
PADDING = 24          # px padding around the shoe on the canvas

IMAGES = [
    "Kayano-31 lightshow.jpeg",
    "Megablast.jpeg",
    "Novablast 5.jpeg",
    "Superblast 2.jpeg",
    "trabuco terra 2.jpeg",
    "metaspeed sky paris.jpeg",
    "trabuco gtx 12.png",
]

base = Path(__file__).parent

for filename in IMAGES:
    src = base / filename
    if not src.exists():
        print(f"  SKIP (not found): {filename}")
        continue

    stem = src.stem
    dest = base / f"{stem}_clean.png"

    print(f"  Processing: {filename} ...", end=" ", flush=True)

    with open(src, "rb") as f:
        raw = f.read()

    result_bytes = remove(raw)

    shoe = Image.open(__import__("io").BytesIO(result_bytes)).convert("RGBA")

    # Crop to the bounding box of non-transparent pixels
    bbox = shoe.getbbox()
    if bbox:
        shoe = shoe.crop(bbox)

    # Scale to fit inside canvas minus padding, keeping aspect ratio
    max_w = CANVAS[0] - PADDING * 2
    max_h = CANVAS[1] - PADDING * 2
    shoe.thumbnail((max_w, max_h), Image.LANCZOS)

    # Centre on transparent canvas
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    x = (CANVAS[0] - shoe.width) // 2
    y = (CANVAS[1] - shoe.height) // 2
    canvas.paste(shoe, (x, y), shoe)

    canvas.save(dest, "PNG")
    print(f"saved → {dest.name} ({canvas.width}×{canvas.height})")

print("Done.")
