"""記事にしてよいかを決める出典の数の規則 (navi-brain#165・K の決定)。

新発売の品は第三者のレビューがまだ少なく、非販売の出典 2 サイトの条件で記事に
ならない。一方で検索の表示を集めているのは新しい ASIN なので、新発売だけ 1 サイトで
よいことにする。品質ゲート (quality_gate.check_sources_v5)・候補の出典待ち
(score_per_asin_info.awaiting_sources)・記事レーンの事前チェック (writer_lane)・
生成前の注記 (build_jules_prompt) がこの関数で同じ値を使う。
"""
import re

# ASIN の先頭 3 文字がこれ以上なら新発売とみなす (Amazon の出品が新しい品)
NEW_PRODUCT_ASIN_FROM = "B0G"
DEFAULT_NON_SALES_SITES = 2
NEW_PRODUCT_NON_SALES_SITES = 1

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def is_new_product(asin) -> bool:
    value = asin.strip().upper() if isinstance(asin, str) else ""
    return bool(_ASIN_RE.match(value)) and value[:3] >= NEW_PRODUCT_ASIN_FROM


def required_non_sales_sites(asin) -> int:
    return NEW_PRODUCT_NON_SALES_SITES if is_new_product(asin) else DEFAULT_NON_SALES_SITES
