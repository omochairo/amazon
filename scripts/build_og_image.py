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
PRODUCT_BOX = 560               # 商品画像 contain-fit box (ほぼ canvas 縦幅まで)
PADDING = 30
BG_COLOR = (255, 255, 255)
BRAND_COLOR = (140, 140, 140)
BRAND_FONT_PX = 22
# 2026-06-03 (#1494 follow-up): X の summary_large_image は image area の下に
# X 自身が twitter:title を別行で描画するため、画像内に title を焼き込むと二重
# 表示になる。タイトル焼き込みを廃止し OG 画像は「視覚要素 (商品画像) + 控えめな
# brand watermark」のみに統一。
BRAND_LINE = "navi.omcha.jp"
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


def build_og_image(
    asin: str,
    image_url: str,
    title: str,
    out_dir: Path = DEFAULT_OUT_DIR,
    force: bool = False,
) -> Path | None:
    """1200x630 OG 画像を out_dir/<asin>.jpg に書き出し、Path を返す。

    既に出力済みで force=False の場合は再生成せずパスのみ返す。
    商品画像が低解像でも 560x560 box に contain-fit して **中央配置 + 白塗り**で
    1.91:1 を満たす。タイトルは焼き込まず、X / Threads の card UI に任せる
    (#1494 初版で焼き込みが twitter:title と二重表示になる問題を受けて廃止)。

    title 引数は API 互換性のため残置 (使用しない)。
    """
    _ = title  # API 互換のため受け取るが使用しない
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
    # canvas 全体の中央 (やや上寄せ) に商品画像を配置。brand watermark 分だけ上に詰める。
    x = (CANVAS_W - src.width) // 2
    y = (CANVAS_H - src.height) // 2 - 18
    canvas.paste(src, (x, y))

    # 右下隅に控えめな brand watermark。font が無い (CJK 不在環境) でも brand 文言は
    # ASCII (navi.omcha.jp) なので Pillow default font に fallback して描画する。
    draw = ImageDraw.Draw(canvas)
    brand_font = _load_font(BRAND_FONT_PX)
    if brand_font is not None:
        try:
            bbox = draw.textbbox((0, 0), BRAND_LINE, font=brand_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            bx = CANVAS_W - text_w - PADDING
            by = CANVAS_H - text_h - PADDING
            draw.text((bx, by), BRAND_LINE, fill=BRAND_COLOR, font=brand_font)
        except Exception as e:
            print(f"build_og_image: brand watermark draw failed for {asin_l}: {e}",
                  file=sys.stderr)
            # brand 無しでも継続 — 商品画像だけで OG として成立する

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
