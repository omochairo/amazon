"""`scripts` を package として import したときに、素の兄弟 import も解決できるようにする。

## 何を直しているか

このディレクトリのモジュールは、兄弟モジュールを 2 形式で参照している。

- `from scripts.quality_gate import ...` … package 形式
- `import stock_status` … `scripts/` が `sys.path` に乗っている前提の素の兄弟 import

本番の呼び出しは `python scripts/foo.py` で、このとき `sys.path[0]` が `scripts/` に
なるため後者が成立する。一方 amazon-home-ops の lane は
`python3 -m scripts.<mod> --help` が通るかをガードにしていて、こちらは `scripts/` が
`sys.path` に乗らないので **素の兄弟 import が ModuleNotFoundError になる**。

2026-08-12 の #5003 が `quality_gate.py` に `import stock_status` を 1 行足しただけで
`24-uniqueness-audit.yml` が 3 週間 (08-16 / 08-23 / 08-30) 緑のまま skip し続けた
(#6504)。同じ壊れ方をしうるモジュールは他に 20 本あった。

## なぜ import 側を全部書き換えるのではなくここで吸収するか

素の兄弟 import は 20 モジュール・数十箇所にあり、1 箇所ずつ try/except に書き換えても
**新しく 1 行足された瞬間に同じ穴が空く**。壊れ方が「無関係な lane が緑のまま止まる」
なので、書き手が気づける保証が無い。形式の違いを package の入口で 1 度だけ吸収する。

`sys.path` への追加は package を import したときにしか起きない。`python scripts/foo.py`
の経路 (本番の主経路) では `__init__.py` は実行されず、従来と何も変わらない。

同名の標準ライブラリを隠す事故は、`scripts/*.py` の名前と `sys.stdlib_module_names` に
重なりが無いことを `scripts/tests/test_package_style_imports.py` で継続的に確認している。
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    # append ではなく insert(0) にしない: 呼び出し側が意図して先頭に置いたパスを
    # 押しのけないため。兄弟 import は他のどこにも無い名前なので末尾で十分。
    _sys.path.append(_HERE)
