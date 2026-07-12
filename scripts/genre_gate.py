"""ジャンル不一致検査ゲート (#2823)。

fetch_amazon.py (取得時ゲート) と scripts/audit_genre_mismatch.py
(既掲載記事の棚卸し) の両方から import される共有モジュール。

per_asin snapshot / search 応答の browse_nodes ([{id, name, root}]) から、
商品が知育玩具メディア (navi.omcha.jp) の対象ジャンルかどうかを判定する。
"""

# 対象ジャンルとみなす root カテゴリ (ancestor チェーン最上位の displayName)。
ALLOWED_ROOTS = {"おもちゃ", "ベビー＆マタニティ", "ベビー&マタニティ"}

# 汎用ストア/プロモノード。実カテゴリではなく販促用の疑似ノードで、
# 手芸用品などの非おもちゃ商品にも付与される (実測: Clover 糸通しが
# 「おもちゃ ストア」を保持)。判定から除外しないと誤 pass する。
# 8184989051=おもちゃ ストア / 5830559051=Toys - AmazonGlobal free shipping /
# 2154250051=キッズのためのお誕生日ストア
STORE_NODE_IDS = {"8184989051", "5830559051", "2154250051"}


def classify_genre(browse_nodes):
    """browse_nodes からジャンル判定を返す。

    Returns:
        (verdict, category_nodes)
        verdict: "pass" | "flag" | "indeterminate"
        category_nodes: 判定に使った実カテゴリノードのリスト (indeterminate なら [])

    - root が null のノードは判定不能として無視する (2026-07-08 の
      browseNodes.ancestor resource 追加より前の snapshot は全ノード root=null)
    - STORE_NODE_IDS は実カテゴリではないので除外
    - 残った実カテゴリノードが 0 件 → "indeterminate" (fail-open で通す)
    - 1 件以上あり、root がひとつも ALLOWED_ROOTS に無い → "flag"
    - ALLOWED_ROOTS の root が 1 つでもあれば → "pass"
    """
    rooted = [nd for nd in (browse_nodes or []) if isinstance(nd, dict) and nd.get("root")]
    cat = [nd for nd in rooted if nd.get("id") not in STORE_NODE_IDS]
    if not cat:
        return "indeterminate", []
    roots = {nd["root"] for nd in cat}
    if roots & ALLOWED_ROOTS:
        return "pass", cat
    return "flag", cat
