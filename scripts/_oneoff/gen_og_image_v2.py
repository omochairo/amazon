"""
Generate 1200x630 OGP image preserving the brand watercolor illustration.

Layout: left side = brand illustration (iroママ+iroパパ+おもちゃ), right side = text.
Source: hugo/static/og-image-source.jpg (1024x1024 original).
Output: hugo/static/og-image.jpg (1200x630, ~1.91:1 spec).
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "hugo", "static", "og-image-source.jpg")
OUT = os.path.join(ROOT, "hugo", "static", "og-image.jpg")

W, H = 1200, 630

# Soft cream / pastel background that matches the watercolor edges
BG = (255, 251, 245)  # near-white cream

img = Image.new("RGB", (W, H), BG)

# --- Left half: brand illustration ---
src = Image.open(SRC).convert("RGB")
# Scale to fit height with a small margin (h=600)
src_h = 600
src_w = int(src.width * src_h / src.height)  # 600 (square)
src = src.resize((src_w, src_h), Image.LANCZOS)

# Place at left with 15px top/bottom margin
left_x = 15
top_y = (H - src_h) // 2
img.paste(src, (left_x, top_y))

# --- Right half: brand text ---
draw = ImageDraw.Draw(img)

# Fonts
font_title = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 60)
font_sub = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 28)
font_meta = ImageFont.truetype("C:/Windows/Fonts/YuGothR.ttc", 22)
font_url = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 24)

# Right column: x=640 to x=1185
RX = 645
RW = W - RX - 30  # available width for text


def text_w(text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


# Color palette
INK = (40, 40, 55)
INK_DIM = (90, 90, 110)
BRAND_ORANGE = (255, 107, 53)
BRAND_TEAL = (46, 196, 182)

# Two-line title for natural line break
title_l1 = "おもちゃいろ"
title_l2 = "比較ナビ"

# Center vertically the right column block
y_title = 145
draw.text((RX, y_title), title_l1, font=font_title, fill=INK)
draw.text((RX, y_title + 72), title_l2, font=font_title, fill=INK)

# divider line
draw.rectangle([(RX, y_title + 162), (RX + 180, y_title + 165)], fill=BRAND_ORANGE)

# Subtitle
y_sub = y_title + 185
draw.text((RX, y_sub), "知育玩具スコア × 3 サイト最安値", font=font_sub, fill=INK_DIM)

# Meta line
y_meta = y_sub + 50
draw.text((RX, y_meta), "Amazon・楽天・Yahoo! を横断比較", font=font_meta, fill=INK_DIM)

# URL bottom-right
url = "navi.omcha.jp"
url_w = text_w(url, font_url)
draw.text((W - url_w - 30, H - 50), url, font=font_url, fill=BRAND_TEAL)

# Save
img.save(OUT, "JPEG", quality=88, optimize=True, progressive=True)
print("wrote", OUT, "size", os.path.getsize(OUT), "bytes")
