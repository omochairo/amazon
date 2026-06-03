"""
Generate a 1200x630 (1.91:1) OGP image for navi.omcha.jp homepage.

Twitter `summary_large_image` cards and Facebook OGP both expect 1.91:1.
Old image was 1024x1024 (1:1) which caused some platforms (LINE / Twitter)
to reject and display a blank placeholder.

Output: hugo/static/og-image.jpg

Run: python scripts/_oneoff/gen_og_image.py
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 630
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "hugo", "static", "og-image.jpg")

# --- background: brand gradient (orange → yellow → teal) ---
img = Image.new("RGB", (W, H), "#FF6B35")
draw = ImageDraw.Draw(img)

# 3-stop diagonal gradient sampled per pixel (slow but one-off)
c1 = (0xFF, 0x6B, 0x35)  # orange
c2 = (0xF7, 0xC9, 0x48)  # yellow
c3 = (0x2E, 0xC4, 0xB6)  # teal


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


px = img.load()
# diagonal direction (135deg)
for y in range(H):
    for x in range(W):
        # progress 0..1 along diagonal
        t = (x + y) / (W + H - 1)
        if t < 0.5:
            color = lerp(c1, c2, t / 0.5)
        else:
            color = lerp(c2, c3, (t - 0.5) / 0.5)
        px[x, y] = color

# subtle dark gradient overlay (top→bottom) for text contrast
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)
for y in range(H):
    alpha = int(70 * (y / H))  # 0 → 70
    od.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
draw = ImageDraw.Draw(img)

# --- main copy ---
font_bold = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 78)
font_sub = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 36)
font_meta = ImageFont.truetype("C:/Windows/Fonts/YuGothR.ttc", 26)

title = "おもちゃいろ 比較ナビ"
sub = "知育玩具スコア × 3 サイト最安値"
meta = "Amazon・楽天・Yahoo! の価格を横断比較"

# helper for text with shadow
def draw_text(xy, text, font, fill=(255, 255, 255), shadow=(0, 0, 0, 160)):
    x, y = xy
    # shadow
    for dx, dy in [(2, 2), (2, 0), (0, 2)]:
        draw.text((x + dx, y + dy), text, font=font, fill=shadow[:3])
    draw.text((x, y), text, font=font, fill=fill)


# Center horizontally
def text_width(text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


# Vertically center the title block
title_w = text_width(title, font_bold)
sub_w = text_width(sub, font_sub)
meta_w = text_width(meta, font_meta)

y0 = 175
draw_text(((W - title_w) // 2, y0), title, font_bold)
draw_text(((W - sub_w) // 2, y0 + 110), sub, font_sub)

# divider
draw.rectangle([((W - 200) // 2, y0 + 175), ((W + 200) // 2, y0 + 178)], fill=(255, 255, 255, 200))

# meta line
draw_text(((W - meta_w) // 2, y0 + 195), meta, font_meta)

# おもちゃロボ監修 badge top-left
# 🤖 emoji は YuGoth に無いので Segoe UI Emoji を embedded_color で別レイヤーに描画
badge_font = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 32)
try:
    emoji_font = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 36)
    emoji_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ed = ImageDraw.Draw(emoji_layer)
    ed.text((40, 30), "🤖", font=emoji_font, embedded_color=True)
    img = Image.alpha_composite(img.convert("RGBA"), emoji_layer).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw_text((92, 38), "おもちゃロボ監修", badge_font)
except Exception as e:
    # フォント無い環境では tofu より文字のみ
    draw_text((48, 38), "おもちゃロボ監修", badge_font)

# domain bottom-right
domain_font = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 28)
domain = "navi.omcha.jp"
dw = text_width(domain, domain_font)
draw_text((W - dw - 48, H - 60), domain, domain_font)

# save with mozjpeg-ish quality
img.save(OUT, "JPEG", quality=88, optimize=True, progressive=True)
print("wrote", OUT, "size", os.path.getsize(OUT))
