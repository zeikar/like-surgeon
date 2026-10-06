#!/bin/sh
# Re-render the README images in docs/assets/ from the HTML sources in this
# directory. Needs Google Chrome (override with CHROME=...) and uv.
set -eu
cd "$(dirname "$0")"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
SHOT="$(mktemp -t likesurgeon-render).png"

render() { # <source>.html <device scale> <output .png or .jpg>
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor="$2" --default-background-color=00000000 \
    --window-size=1280,1400 --virtual-time-budget=5000 \
    --screenshot="$SHOT" "file://$PWD/$1.html" 2>/dev/null
  # The window is taller than the page: crop the transparent margin.
  uv run --quiet --no-project --with pillow python -c '
import sys
from PIL import Image
src, out = sys.argv[1:]
im = Image.open(src)
im = im.crop(im.getchannel("A").getbbox())
if out.endswith(".jpg"):
    im.convert("RGB").save(out, quality=85, optimize=True, progressive=True)
else:
    im.save(out, optimize=True)
' "$SHOT" "$3"
}

render how-it-works 2 ../how-it-works.png
render terminal 2 ../terminal.png
render hero 2 ../hero.jpg
# GitHub social preview: 1280x640, under 1 MB. Upload it by hand in Settings.
render hero 1 ../social-preview.jpg
rm -f "$SHOT"
