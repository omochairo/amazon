"""strip_brand_narrative_cta.py

#5330 — ブランドハブ narrative の最終段落 (本サイト CTA 固定文) を本文から外す。

`hugo/content/brands/<slug>/_index.md` の narrative は
BRAND_NARRATIVE_PROMPT.md の「段落 4: 本サイト CTA (固定文)」を毎回含んでおり、
84 本すべてがブランド名だけ違う同一文だった。同じ文言を
`hugo/layouts/partials/brand_hub_cta.html` がテンプレート側で出すようにしたので、
本文からは落とす (残すと同じ段落が 2 回出る)。

生成元プロンプト側 (private repo) も同時に直すが、Jules の PR が古いプロンプトで
走った場合や過去分の取りこぼしに備えて、何度でも安全に流せる形にしてある。

CLI:
    python scripts/strip_brand_narrative_cta.py            # 書き換える
    python scripts/strip_brand_narrative_cta.py --check    # 検出のみ (CI 用, 残っていれば exit 1)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_BRANDS_DIR = _REPO_ROOT / "hugo" / "content" / "brands"

# 実データで確認した表記ゆれ:
#   - 「おもちゃロボ独自」/「編集部独自」の 2 通り
#   - 「4 軸)」の直後に改行が入るもの / 入らないもの
#   - ブランド名は任意 (「で<ブランド>商品を横断比較」)
_CTA_RE = re.compile(
    r"本サイトでは、(?:おもちゃロボ|編集部)独自の知育スコア\s*"
    r"\(教育性\s*/\s*長寿命性\s*/\s*安全性\s*/\s*コストパフォーマンスの\s*4\s*軸\)\s*"
    r"で.*?商品を横断比較。.*?最安値で\s*見つけられます。",
    re.DOTALL,
)


def strip_cta(text: str) -> tuple[str, bool]:
    """本文から CTA 段落を落とした文字列と、落としたかどうかを返す。

    段落単位で判定する (文中の一部だけ消して不完全な文を残さないため)。
    """
    # front matter は触らない。--- で 3 分割し body だけを対象にする。
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            head, body = "---" + parts[1] + "---", parts[2]
        else:
            return text, False
    else:
        head, body = "", text

    paragraphs = body.split("\n\n")
    kept = [p for p in paragraphs if not _CTA_RE.search(p)]
    if len(kept) == len(paragraphs):
        return text, False

    new_body = "\n\n".join(kept).rstrip() + "\n"
    if head and not new_body.startswith("\n"):
        new_body = "\n" + new_body
    return head + new_body, True


def iter_index_files(brands_dir: pathlib.Path | str) -> list[pathlib.Path]:
    root = pathlib.Path(brands_dir)
    if not root.exists():
        return []
    return sorted(p for p in root.glob("*/_index.md"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brands-dir", default=str(_DEFAULT_BRANDS_DIR))
    p.add_argument("--check", action="store_true",
                   help="書き換えずに検出だけ行う (残っていれば exit 1)")
    args = p.parse_args(argv)

    hits: list[str] = []
    for f in iter_index_files(args.brands_dir):
        text = f.read_text(encoding="utf-8")
        new, changed = strip_cta(text)
        if not changed:
            continue
        hits.append(f.parent.name)
        if not args.check:
            f.write_text(new, encoding="utf-8")

    verb = "found" if args.check else "stripped"
    print(f"[strip_brand_narrative_cta] {verb} {len(hits)} file(s)")
    for name in hits:
        print(f"  - {name}")
    if args.check and hits:
        print(
            "CTA 段落が本文に残っています。"
            "partials/brand_hub_cta.html と二重に出るので、"
            "python scripts/strip_brand_narrative_cta.py を流してください。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
