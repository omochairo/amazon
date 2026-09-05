"""`scripts/` のモジュールが package 形式 (`python -m scripts.X`) でも import できることを守る。

## なぜ要るか (2026-09-05)

このディレクトリのモジュールは兄弟モジュールを 2 形式で参照している。

- `from scripts.quality_gate import ...` … package 形式
- `import stock_status` … `scripts/` が sys.path に乗っている前提の素の兄弟 import

本番の主経路 `python scripts/foo.py` では後者が成立するが、amazon-home-ops の lane は
`python3 -m scripts.<mod> --help` が通るかをガードにしていて、そこでは成立しない。

実害: 2026-08-12 の #5003 が `quality_gate.py` に `import stock_status` を 1 行足した
結果、`audit_uniqueness` の import 木が切れ、`24-uniqueness-audit.yml` が 08-16 /
08-23 / 08-30 の 3 run とも **success のまま何もせず** skip した。凡庸度監査 (#3300) の
データは 2026-W32 で止まり、気づいたのは 4 週間後。

- unit test は `sys.path` に `scripts/` を足す形式で書かれているので **1 件も落ちない**
- freshness 監視 (#4789) はこのファイルを UNMONITORED にしているので掛からない
- run は success なので run 一覧でも異常に見えない

検知経路が 1 本も無かった。形式の吸収は `scripts/__init__.py` が担い、ここはそれが
効き続けていることを確認する側。

## 何を守るか

1. `scripts/*.py` が **全部** package 形式で import できる (新規モジュールも自動で対象)
2. home-ops の lane がガードに使っている個別モジュール (落ちたときにどの lane が
   止まるかを名指しで出すため、1 に含まれていても別に持つ)
3. `scripts/__init__.py` が sys.path を伸ばすので、標準ライブラリと同名のファイルを
   置くと全 import が壊れる。名前の重なりが無いことを継続的に見る
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _module_names() -> list[str]:
    return sorted(
        p.stem for p in SCRIPTS_DIR.glob("*.py") if not p.stem.startswith("__")
    )


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
def test_guarded_module_importable_as_package(module: str) -> None:
    """home-ops の lane がガードに使うモジュールが package 形式で import できること。

    subprocess で分離するのは、テストプロセス側が既に `sys.path` に `scripts/` を
    足していて素の兄弟 import が**通ってしまう**ため。lane と同じ条件で見るには
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


def test_all_scripts_importable_as_package() -> None:
    """`scripts/*.py` が全部 package 形式で import できること。

    ガード対象は増減するので、名指しのリストだけでは新しい穴を拾えない。判定は
    `_package_import_probe.py` に置いた子プロセスが 1 本ずつ独立条件で行う
    (同一プロセスで素朴に回すと順序依存で全部通ってしまう。理由はそちらの docstring)。
    """
    probe = pathlib.Path(__file__).with_name("_package_import_probe.py")
    r = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, cwd=str(REPO_ROOT)
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    failures = r.stdout.decode("utf-8", "replace").strip()
    assert not failures, (
        "package 形式で import できないモジュールがある。素の兄弟 import を足した場合は "
        "scripts/__init__.py の吸収が効いているか確認すること:\n" + failures
    )


def test_no_stdlib_name_shadowing() -> None:
    """標準ライブラリと同名のファイルを `scripts/` に置かないこと。

    `scripts/__init__.py` が `sys.path` に `scripts/` を足すため、同名ファイルがあると
    それを import した全モジュールが壊れる。追加は末尾 (append) なので通常は標準側が
    勝つが、`sys.path` の並びに依存する壊れ方は再現条件が読みにくいので名前の側で防ぐ。
    """
    overlap = sorted(set(_module_names()) & set(sys.stdlib_module_names))
    assert not overlap, f"標準ライブラリと同名: {overlap}"
