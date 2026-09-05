"""amazon-home-ops の lane が `python3 -m scripts.<mod>` でガードしているモジュールが、
package 形式のまま import できることを守る回帰テスト。

## なぜ要るか (2026-09-05)

`scripts/` には `__init__.py` が無く、兄弟モジュールの参照が 2 形式で混在している。

- `from scripts.quality_gate import ...` … package 形式
- `import stock_status` … `scripts/` が sys.path に乗っている前提の素の兄弟 import

後者は `python -m scripts.X` では解決できないので、**package 形式で import される側の
モジュールに素の兄弟 import が 1 行入るだけで、その import 木全体が壊れる**。

これが本番で効いた実例: 2026-08-12 の #5003 が `scripts/quality_gate.py` に
`import stock_status` を足した。`audit_uniqueness` は `from scripts.quality_gate import ...`
なので、`python3 -m scripts.audit_uniqueness --help` が ModuleNotFoundError になった。
amazon-home-ops の `24-uniqueness-audit.yml` はこの可否をガードにしていて、

    scripts.audit_uniqueness not importable on main; skipping

と出して **run は success で終わる**。結果、凡庸度監査 (#3300) は 08-16 / 08-23 / 08-30 の
3 run すべてが緑のまま何もせず、データは 2026-W32 (08-09) で止まっていた。
気づいたのは 4 週間後。freshness 監視 (#4789) はこのファイルを UNMONITORED にしているので
そこにも掛からない。

unit test は `sys.path` に `scripts/` を足す形式で書かれているため、**この壊れ方は
既存のテストを 1 件も落とさない**。だから形式を明示的に固定するテストが要る。

## 何を守るか

ここに挙げるのは「別リポジトリの lane が package 形式で叩いているモジュール」だけ。
`scripts/` 全体を package 形式へ寄せるのは別件 (import 形式の統一は #6503)。
lane のガード対象が増えたらここにも足す。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# amazon-home-ops の workflow が `python3 -m scripts.<mod>` で叩いているモジュール。
# 取得元 (2026-09-05 時点):
#   22-answerability-audit.yml … audit_query_entailment / comment_answerability_audit
#   24-uniqueness-audit.yml    … audit_uniqueness / append_uniqueness_audit_history
#   26-faq-seo-lane.yml        … generate_faq_seo / generate_internal_links
#   27-wp-navi-link-lane.yml   … build_wp_navi_link_candidates
GUARDED_MODULES = [
    "append_uniqueness_audit_history",
    "audit_query_entailment",
    "audit_uniqueness",
    "build_wp_navi_link_candidates",
    "comment_answerability_audit",
    "generate_faq_seo",
    "generate_internal_links",
]


@pytest.mark.parametrize("module", GUARDED_MODULES)
def test_importable_as_package_module(module: str) -> None:
    """`import scripts.<mod>` が通ること。

    subprocess で分離するのは、テストプロセス側が既に `sys.path` に `scripts/` を
    足していて、素の兄弟 import が**通ってしまう**ため。lane と同じ条件で見るには
    汚れていない interpreter が要る。
    """
    r = subprocess.run(
        [sys.executable, "-c", f"import scripts.{module}"],
        capture_output=True,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"scripts.{module} が package 形式で import できない。"
        f"home-ops の lane が緑のまま skip する:\n"
        + r.stderr.decode("utf-8", "replace")[-1500:]
    )
