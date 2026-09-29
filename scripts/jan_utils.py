"""JAN / EAN / UPC コードの小さな共通処理。

`fetch_amazon.extract_jan` は eans が無いと upcs を拾うので (#2747)、輸入品では
12 桁の UPC-A が `jan_code` に入る。保存データはそのままにして、外部 API に渡す
直前や、別の出どころの JAN と突き合わせる直前にだけこのモジュールで正規化する。
"""

from __future__ import annotations


def to_ean13(code):
    """12 桁数字の UPC-A を先頭 0 付きの EAN-13 にする。それ以外はそのまま返す。

    楽天 Books `isbnjan` は 13 桁以外を 400 で弾き、Yahoo の `jan_code` 検索も
    12 桁だと 400 を返す (#8547)。UPC-A → EAN-13 は先頭に 0 を足すだけで
    チェックディジットも変わらない。全角数字は `str.isdigit()` が True になるので
    ASCII に限る。
    """
    if isinstance(code, str) and len(code) == 12 and code.isascii() and code.isdigit():
        return "0" + code
    return code
