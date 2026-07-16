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

# root は ALLOWED_ROOTS 外だが、実態が知育玩具であるノード (2026-07-16 の
# 全量棚卸しで誤検知と確認したものを昇格)。root だけ見るとホビー/文房具/楽器に
# 属するが、商品としては当サイトの対象。
ALLOWED_NODE_IDS = {
    "2189574051": "ミニカー・ダイキャストカー(ホビー)",
    "2189356051": "フィギュア・コレクタードール(ホビー)",
    "14616501051": "キット(ホビー)",
    "3429044051": "カリンバ(楽器・音響機器)",
    "89393051": "工作用品(文房具・オフィス用品)",
    "4095902051": "学習帳・練習帳(文房具・オフィス用品)",
    "16391847051": "授業記録・レッスンブック(文房具・オフィス用品)",
}

# ノード単位では救えない個別の誤検知。対象ノードを非おもちゃ商品と共有して
# いるため ALLOWED_NODE_IDS に入れると誤 pass が増える場合にのみ使う。
ALLOWED_ASINS = {
    # 10532098051「置物・オブジェ」を、削除対象の B0G4WCS2XX (おみやげキャップ)
    # と共有している。
    "B0GC4MQL8N": "りぶはあと ぬいぐるみマスコット はむにぎり",
    # 3054990051「手芸キット」は大人向け裁縫道具も含む。同ノードを理由に
    # blocklist 済みの B07TL3JZGH (清原 ループ返し) があり、ノード許可すると
    # 2026-07-12 の手芸用品 cleanup を無効化してしまう。
    "B096TWPGWV": "サンフェルト 子ども手芸キット ゆめいろマスコット",
}


def classify_genre(browse_nodes, asin=None):
    """browse_nodes からジャンル判定を返す。

    Returns:
        (verdict, category_nodes)
        verdict: "pass" | "flag" | "indeterminate"
        category_nodes: 判定に使った実カテゴリノードのリスト (indeterminate なら [])

    - asin が ALLOWED_ASINS にあれば無条件 "pass"
    - root が null のノードは判定不能として無視する (2026-07-08 の
      browseNodes.ancestor resource 追加より前の snapshot は全ノード root=null)
    - STORE_NODE_IDS は実カテゴリではないので除外
    - 残った実カテゴリノードが 0 件 → "indeterminate" (fail-open で通す)
    - ALLOWED_ROOTS の root か ALLOWED_NODE_IDS のノードが 1 つでもあれば → "pass"
    - どちらも無ければ → "flag"
    """
    rooted = [nd for nd in (browse_nodes or []) if isinstance(nd, dict) and nd.get("root")]
    cat = [nd for nd in rooted if nd.get("id") not in STORE_NODE_IDS]
    if asin and asin in ALLOWED_ASINS:
        return "pass", cat
    if not cat:
        return "indeterminate", []
    if {nd["root"] for nd in cat} & ALLOWED_ROOTS:
        return "pass", cat
    if {nd.get("id") for nd in cat} & set(ALLOWED_NODE_IDS):
        return "pass", cat
    return "flag", cat
