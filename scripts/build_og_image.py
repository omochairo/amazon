#!/usr/bin/env python3
"""1200x630 OG/Twitter Card 画像を商品画像 + タイトルで合成する (Issue #1494)。

Amazon CDN は商品によって元画像が 500x455 程度しかなく、og:image:width=1200 を
宣言していても X (summary_large_image) / Threads / FB で preview が劣化または
抑止される。本モジュールは build 時に 1200x630 (1.91:1) の専用 OG 画像を
合成して `hugo/static/og/<asin>.jpg` に出力する。

Layout (1200x630, 白背景):
    +---------------------------+----------------------------+
    |                           |                            |
    |    [PRODUCT IMAGE]        |   TITLE (3 行まで wrap)    |
    |    540x540 box contain    |                            |
    |    左半 centered          |   おもちゃいろ 比較ナビ      |
    |                           |   (small brand line)       |
    +---------------------------+----------------------------+

Usage (CLI / backfill):
    python scripts/build_og_image.py <ASIN>            # 1 件
    python scripts/build_og_image.py --all             # 全 article を backfill

build_post.py からは `build_og_image(asin, image_url, title, out_dir)` を呼ぶ。

Fail-soft:
    - 商品画像 fetch 失敗 / Pillow 不在 / font 不在 → None を返し caller は
      従来挙動 (Amazon 画像 URL を直接 og:image に) に fallback する。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"
DEFAULT_OUT_DIR = REPO_ROOT / "hugo" / "static" / "og"

CANVAS_W = 1200
CANVAS_H = 630
PRODUCT_BOX = 540               # 商品画像を contain-fit する正方形 box
LEFT_PANE_W = 600               # 左半 = 商品画像用
PADDING = 30
BG_COLOR = (255, 255, 255)
TITLE_COLOR = (40, 40, 40)
BRAND_COLOR = (110, 110, 110)
TITLE_FONT_PX = 44              # 3 行で約 14 文字/行 (CJK)
BRAND_FONT_PX = 24
BRAND_LINE = "おもちゃいろ 比較ナビ"
HTTP_TIMEOUT = 20
JPEG_QUALITY = 85

# Linux (GitHub Actions ubuntu-latest, after `apt-get install fonts-noto-cjk`) /
# Windows ローカル開発で利用可能な日本語 font 候補。順に試して見つかった最初を使う。
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "C:\\Windows\\Fonts\\YuGothB.ttc",
    "C:\\Windows\\Fonts\\meiryob.ttc",
    "C:\\Windows\\Fonts\\msgothic.ttc",
]


def _find_font_path() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _load_font(size: int):
    # 遅延 import: Pillow 不在環境 (validate smoke test など) で import エラーで
    # build_post 全体が落ちないように caller 側で例外捕捉する。
    from PIL import ImageFont  # type: ignore

    path = _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return None  # CJK font 不在 → caller が build_og_image を skip して fallback


def _fetch_image_bytes(url: str) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "omochairo-build-og/1.0 (+https://navi.omcha.jp)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"build_og_image: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def _wrap_title(draw, text: str, font, max_w: int, max_lines: int = 3) -> list[str]:
    """CJK 対応の素朴な wrap。1 文字ずつ width を測って max_w を超えたら改行。

    PIL の textbbox は font kerning も含むので空白を意識せず詰めて測定する。
    """
    lines: list[str] = []
    buf = ""
    for ch in text:
        candidate = buf + ch
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_w and buf:
            lines.append(buf)
            buf = ch
            if len(lines) == max_lines - 1:
                # 最終行: 残り全部詰めて末尾を ellipsis で truncate
                rest = ch
                for ch2 in text[text.index(ch) + 1:]:
                    candidate2 = rest + ch2
                    bbox2 = draw.textbbox((0, 0), candidate2 + "…", font=font)
                    if bbox2[2] - bbox2[0] > max_w:
                        rest = rest + "…"
                        break
                    rest = candidate2
                lines.append(rest)
                return lines
        else:
            buf = candidate
    if buf:
        lines.append(buf)
    return lines


def build_og_image(
    asin: str,
    image_url: str,
    title: str,
    out_dir: Path = DEFAULT_OUT_DIR,
    force: bool = False,
) -> Path | None:
    """1200x630 OG 画像を out_dir/<asin>.jpg に書き出し、Path を返す。

    既に出力済みで force=False の場合は再生成せずパスのみ返す (build_post の
    毎回 invocation を高速化)。
    商品画像が低解像でも 540x540 box に contain-fit して中央配置 + 余白白塗りする
    ので X / Threads の 1.91:1 要件を満たす。
    """
    if not asin or not image_url:
        return None
    asin_l = asin.lower()
    out_path = out_dir / f"{asin_l}.jpg"
    if out_path.exists() and not force:
        return out_path

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError as e:
        print(f"build_og_image: Pillow not available ({e})", file=sys.stderr)
        return None

    raw = _fetch_image_bytes(image_url)
    if not raw:
        return None

    try:
        src = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        print(f"build_og_image: open failed for {asin_l}: {e}", file=sys.stderr)
        return None

    # contain-fit into PRODUCT_BOX (アスペクト保持、box に納まるよう縮小、拡大はしない)
    src.thumbnail((PRODUCT_BOX, PRODUCT_BOX), Image.LANCZOS)

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
    # 左半 600px の中央に商品画像を配置
    x = (LEFT_PANE_W - src.width) // 2
    y = (CANVAS_H - src.height) // 2
    canvas.paste(src, (x, y))

    draw = ImageDraw.Draw(canvas)
    title_font = _load_font(TITLE_FONT_PX)
    brand_font = _load_font(BRAND_FONT_PX)
    if title_font is None or brand_font is None:
        # CJK font が見つからない環境では日本語が tofu になるので OG 生成を諦め、
        # caller (build_post) が Amazon 画像 URL に fallback する。
        print(
            f"build_og_image: no CJK font found (tried {len(FONT_CANDIDATES)} candidates); "
            f"skipping {asin_l}",
            file=sys.stderr,
        )
        return None

    # 右半: タイトル wrap (max 3 行) + 下部に brand line
    right_x = LEFT_PANE_W + PADDING
    right_w = CANVAS_W - right_x - PADDING

    lines = _wrap_title(draw, title or "", title_font, right_w, max_lines=3)
    line_h = int(TITLE_FONT_PX * 1.35)
    block_h = line_h * len(lines)
    title_y = (CANVAS_H - block_h) // 2 - 30  # brand 用に少し上寄せ
    for i, line in enumerate(lines):
        draw.text((right_x, title_y + i * line_h), line, fill=TITLE_COLOR, font=title_font)

    brand_y = CANVAS_H - BRAND_FONT_PX - PADDING - 10
    draw.text((right_x, brand_y), BRAND_LINE, fill=BRAND_COLOR, font=brand_font)

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        canvas.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    except Exception as e:
        print(f"build_og_image: save failed for {asin_l}: {e}", file=sys.stderr)
        return None
    return out_path


def _load_article(asin: str) -> dict | None:
    pattern = str(ARTICLES_DIR / f"*-{asin.upper()}.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return None
    with open(matches[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def _build_one(asin: str, force: bool) -> int:
    article = _load_article(asin)
    if not article:
        print(f"no article JSON for ASIN={asin}", file=sys.stderr)
        return 1
    product = article.get("product") or {}
    image_url = product.get("image") or ""
    title = article.get("title") or ""
    if not image_url:
        print(f"no product.image for ASIN={asin}", file=sys.stderr)
        return 1
    out = build_og_image(asin, image_url, title, force=force)
    if out:
        print(f"OK {asin}: {out}")
        return 0
    print(f"FAIL {asin}", file=sys.stderr)
    return 1


def _build_all(force: bool) -> int:
    fail = 0
    paths = sorted(ARTICLES_DIR.glob("*.json"))
    for jp in paths:
        # ファイル名末尾の -<ASIN>.json から抜く
        stem = jp.stem
        if "-" not in stem:
            continue
        asin = stem.rsplit("-", 1)[-1]
        if not asin.startswith("B0"):
            continue
        rc = _build_one(asin, force)
        if rc != 0:
            fail += 1
    print(f"--- done: {len(paths)} articles, {fail} failed ---")
    return 0 if fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Build 1200x630 OG image for an ASIN article")
    parser.add_argument("asin", nargs="?", help="Amazon ASIN (omit with --all)")
    parser.add_argument("--all", action="store_true", help="Backfill all articles under data/articles/")
    parser.add_argument("--force", action="store_true", help="Regenerate even if output exists")
    args = parser.parse_args()
    if args.all:
        return _build_all(args.force)
    if not args.asin:
        parser.error("ASIN required (or use --all)")
    return _build_one(args.asin.upper(), args.force)


if __name__ == "__main__":
    sys.exit(main())
