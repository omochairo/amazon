"""`scripts/*.py` を 1 本ずつ独立条件で package 形式 import する子プロセス。

`test_package_style_imports.py` から呼ばれる。テスト本体に置かないのは、pytest の
プロセスが既に `sys.path` に `scripts/` を足しており、そこでは素の兄弟 import が
通ってしまって何も検出できないため。

## 同一プロセスで素朴に回してはいけない

`jules_quota_gate` のように `sys.path.insert(dirname(__file__))` する保険を持つ
モジュールが 1 本でも先に import されると、以降の素の兄弟 import が全部そこで
解決してしまう。**実測: この後始末が無いと 20 本の欠陥が 0 件に見えた** (2026-09-05)。

そこで 1 本ごとに
  - `sys.path` を初期状態へ戻す
  - `scripts/` 配下から読み込まれた module を `sys.modules` から落とす
を行う。モジュールごとに interpreter を起こすと数分掛かるので、分離はこの 1 プロセス
ぶんに留める (172 本で 2 秒程度)。

出力: import に失敗したモジュールを 1 行 1 件で stdout に出す。0 件なら空。
"""
from __future__ import annotations

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

# `python <path>` で起動されるため sys.path[0] はこのファイルの居場所になる。
# lane と同じ条件 (repo root だけが乗っている状態) に揃える。
BASE_PATH = [REPO_ROOT] + [p for p in sys.path[1:] if os.path.abspath(p or ".") != _HERE]
BASE_MODULES = set(sys.modules)


def _reset() -> None:
    sys.path[:] = list(BASE_PATH)
    for name in list(sys.modules):
        if name in BASE_MODULES:
            continue
        mod = sys.modules.get(name)
        origin = getattr(mod, "__file__", None) or ""
        if (
            name == "scripts"
            or name.startswith("scripts.")
            or origin.startswith(SCRIPTS_DIR)
        ):
            del sys.modules[name]


def main() -> int:
    modules = sorted(
        f[:-3]
        for f in os.listdir(SCRIPTS_DIR)
        if f.endswith(".py") and not f.startswith("__")
    )
    failures = []
    for m in modules:
        _reset()
        try:
            importlib.import_module("scripts." + m)
        except Exception as e:  # noqa: BLE001 — import できるかどうかだけを見る
            failures.append(f"{m}: {type(e).__name__}: {e}")
    _reset()
    sys.stdout.write("\n".join(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
