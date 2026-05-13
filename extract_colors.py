"""Extract dominant accent colors from a shoe PNG (transparent background expected)."""
import sys
from pathlib import Path
from PIL import Image
import collections

def rgb_to_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"

def is_neutral(r, g, b, sat_threshold=40, bright_max=240, bright_min=25):
    """True if the pixel is near-white, near-black, or near-gray."""
    brightness = (r + g + b) / 3
    saturation = max(r, g, b) - min(r, g, b)
    return saturation < sat_threshold or brightness > bright_max or brightness < bright_min

def quantize_channel(v, step=24):
    return (v // step) * step

def extract_colors(image_path, n=8):
    img = Image.open(image_path).convert("RGBA")
    pixels = list(img.getdata())

    opaque = [(r, g, b) for r, g, b, a in pixels if a > 128]
    if not opaque:
        print("No opaque pixels found.")
        return []

    # Try strict filter first; fall back to lenient for near-neutral shoes
    colorful = [p for p in opaque if not is_neutral(*p)]
    if len(colorful) < 500:
        colorful = [p for p in opaque if not is_neutral(*p, sat_threshold=12, bright_max=252, bright_min=10)]
    if not colorful:
        print("No colorful pixels found.")
        return []

    # Bucket into coarse bins to find dominant hues
    buckets = collections.Counter(
        (quantize_channel(r), quantize_channel(g), quantize_channel(b))
        for r, g, b in colorful
    )

    # Pick top N buckets, then find actual median color within each bucket
    top = []
    for (br, bg, bb), count in buckets.most_common(n * 4):
        members = [
            (r, g, b) for r, g, b in colorful
            if quantize_channel(r) == br and quantize_channel(g) == bg and quantize_channel(b) == bb
        ]
        med_r = sorted(m[0] for m in members)[len(members) // 2]
        med_g = sorted(m[1] for m in members)[len(members) // 2]
        med_b = sorted(m[2] for m in members)[len(members) // 2]
        top.append((count, med_r, med_g, med_b))

    # Deduplicate similar colors (keep most-common within each 48-step bucket)
    seen = set()
    results = []
    for count, r, g, b in sorted(top, reverse=True):
        coarse = (r // 48, g // 48, b // 48)
        if coarse not in seen:
            seen.add(coarse)
            results.append((count, r, g, b))
        if len(results) >= n:
            break

    return results

if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("metaspeed sky paris_clean.png")
    print(f"Extracting colors from: {path.name}\n")
    colors = extract_colors(path)
    total = sum(c for c, *_ in colors)
    for count, r, g, b in colors:
        hex_color = rgb_to_hex(r, g, b)
        pct = count / total * 100
        print(f"  {hex_color}  ({r:3d},{g:3d},{b:3d})  {pct:5.1f}%  {'█' * int(pct / 2)}")
