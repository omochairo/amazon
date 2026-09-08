"""check_article_pr_deletions.py

記事 PR (data/articles/*.json を追加/変更する PR) が data/articles/ 以外の
ファイルを削除していないかを判定する (#6793 の catch 層)。

#6788 で Jules の修正コミットが data/raw/per_asin/<ASIN>/omcha_related.json を
巻き添えで削除したが、PR の files API では追加ファイルと相殺されて完全に
不可視だった。prevent 側は #6792 (--match-head-commit) で対応済みなので、
ここでは「削除が起きたら気付けるようにする」ことだけをやる。

使い方 (.github/workflows/04-validate-article-pr.yml):

    git diff --name-only --diff-filter=D "$base" "$head" \
      | python scripts/check_article_pr_deletions.py

終了コード: data/articles/ 配下以外の削除が 0 件なら 0、1 件以上あれば 1。
"""
from __future__ import annotations

import sys

ALLOWED_PREFIX = "data/articles/"


def disallowed_deletions(deleted_paths: list[str]) -> list[str]:
    """許可プレフィックス外の削除パスを返す (順序は入力順を保持)。"""
    return [p for p in deleted_paths if p and not p.startswith(ALLOWED_PREFIX)]


def main() -> int:
    deleted_paths = [line.strip() for line in sys.stdin if line.strip()]
    bad = disallowed_deletions(deleted_paths)

    if not bad:
        print(f"[check_article_pr_deletions] OK: no deletions outside {ALLOWED_PREFIX}")
        return 0

    print(
        f"[check_article_pr_deletions] FAIL: {len(bad)} file(s) deleted outside "
        f"{ALLOWED_PREFIX}:",
        file=sys.stderr,
    )
    for p in bad:
        print(f"  {p}", file=sys.stderr)
    print(
        "記事 PR は data/articles 以外を削除しない。Jules の修正コミットが "
        "巻き添えで消していないか確認せよ (#6793)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
