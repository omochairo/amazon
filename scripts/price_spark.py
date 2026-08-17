"""price_spark.py — 価格推移スパークラインのジオメトリ計算 (SSOT)。

#5120 / #5135 で商品ページ用に作り込んだ描画ロジックを、一覧ページ
(/price/ /deals/) のカードからも使えるように build_post.py から切り出したもの。
呼び出し側は build_post.py (ARTICLE_GEOM) / build_price_dashboard.py /
build_feature_lists.py (CARD_GEOM)。

ジオメトリをモジュール定数でなく SparkGeom で受け取るのは、商品ページ版
(軸ラベル・凡例・観測ドットあり、300x90) とカード版 (線だけ、160x40) で
同じ階段線ロジックを共有しつつ寸法だけ差し替えるため。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# 一覧カードにスパークラインを出す最低条件 (#5120 の商品ページ側とは別基準)。
# CARD_MIN_POINTS: 2点では「線」にはなるが推移として読めない。
# CARD_MIN_DISTINCT_PRICES: 価格の種類が1つだと必ず水平な棒になり情報量ゼロ。
#   実測 (2026-08-13) では /deals/ 20件中13件がこれに該当したため、無条件描画だと
#   カードの2/3がただの横棒になる。
CARD_MIN_POINTS = 3
CARD_MIN_DISTINCT_PRICES = 2


@dataclass(frozen=True)
class SparkGeom:
    width: int
    height: int
    plot_x_min: float
    plot_x_max: float
    plot_y_top: float
    plot_y_bottom: float
    gap_days: int
    show_dots: bool
    show_labels: bool
    show_legend: bool
    y_label_x: float
    y_label_max_y: float
    y_label_min_y: float
    x_label_y: float
    legend_y: float
    legend_dash_len: float
    legend_text_gap: float
    legend_text: str
    # --- 以下は #5130 項目2 で追加 ---
    # 在庫切れ区間の凡例テキスト。未観測 (legend_text) と同時に出ることがある。
    legend_text_out_of_stock: str = "点線＝在庫切れ期間"
    # 凡例を横に並べるときの 1 エントリぶんの送り幅。テキスト長は font-size 9 の
    # 日本語 8 文字ぶん (約 72px) を上限に見ておく。
    legend_advance: float = 108.0
    # --- 以下は #5167 で追加。既定値は商品ページ (ARTICLE_GEOM) の従来挙動 ---
    # y 軸ラベルの寄せ。商品ページはプロット左に置くので "end"、カードは右に
    # 置いて横幅を稼ぐので "start"。
    y_label_anchor: str = "end"
    # x 軸ラベルの日付書式。"iso" = 2026-08-14 (商品ページ)、
    # "short" = 8/14 (カード。桁数を削って小さい図でも読めるようにする)。
    # strftime の %-m はプラットフォーム依存なので自前で組む。
    date_style: str = "iso"
    # 折れ線の下を塗るか。カードでは「線が1本あるだけ」に見えるのを避けるため塗る。
    show_area: bool = False
    # 最新観測点にマーカーを打つか (「今どこにいるか」を示す)。
    show_last_marker: bool = False
    # 座標の小数桁数 (#5225)。カードは全ページに最大150枚並ぶので、points 文字列が
    # そのままページ重量になる。viewBox 220x72 を 220px で描くので 1 単位 = 約1px、
    # 整数に丸めても誤差は 0.5px 未満で見た目に影響しない。商品ページ (300x90、
    # 1ページ1枚) は従来どおり小数1桁のままにして出力を変えない。
    coord_precision: int = 1


ARTICLE_GEOM = SparkGeom(
    width=300,
    height=90,
    plot_x_min=52.0,
    plot_x_max=296.0,
    plot_y_top=8.0,
    plot_y_bottom=56.0,
    gap_days=14,
    show_dots=True,
    show_labels=True,
    show_legend=True,
    y_label_x=48.0,
    y_label_max_y=11.0,
    y_label_min_y=59.0,
    x_label_y=74.0,
    legend_y=86.0,
    legend_dash_len=16.0,
    legend_text_gap=4.0,
    legend_text="破線＝未観測期間",
)

# 一覧カード用 (#5167 で再設計)。
#
# 初版 (#5143) は「線だけ・軸ラベルなし・160x40」だった。これは失敗で、カード上では
# 意味の分からない線が1本あるだけに見え、読者に何も伝わっていなかった。
#
# 商品ページのグラフ (#5120) と一覧カードのグラフは目的が違う:
#   商品ページ … Keepa グラフの隣に置き、「このサイトは自前で価格を観測している」
#                という事実を検索エンジンと読者に示す証跡。日付も絶対値も要る
#   一覧カード … 「この商品の価格がどう動いて今いくらか」を一目で伝える。
#                カード内の限られた面積で、価格の上下と現在位置が読めればよい
#
# そこでカード版は「軸ラベルは残すが最小限、面で塗って形を読ませ、最新点を打つ」に
# する。y ラベルはプロットの右に置いて横幅を稼ぎ、日付は M/D に詰める。
CARD_GEOM = SparkGeom(
    width=220,
    height=72,
    plot_x_min=3.0,
    plot_x_max=163.0,
    plot_y_top=13.0,
    plot_y_bottom=47.0,
    gap_days=14,
    show_dots=False,          # 観測点を全部打つと小さい図では潰れるので最新点だけ
    show_labels=True,
    show_legend=False,        # 凡例の代わりに破線区間の説明は <desc> に持たせる
    y_label_x=168.0,
    y_label_max_y=17.0,
    y_label_min_y=51.0,
    x_label_y=63.0,
    legend_y=0.0,
    legend_dash_len=0.0,
    legend_text_gap=0.0,
    legend_text="",
    y_label_anchor="start",
    date_style="short",
    show_area=True,
    show_last_marker=True,
    coord_precision=0,
)


def parse_ts(ts: str) -> Optional[datetime]:
    """観測点の ``ts`` (ISO8601, ``Z`` 表記あり) を aware datetime にパースする。

    失敗したら None（呼び出し側でその点をスパーク描画から除外する）。
    """
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# セグメントの状態 (#5130 項目2)。#5120 の observed:bool を 3 値に広げたもの。
#   observed     … 観測点どうしを結ぶ実線 (価格が記録されている)
#   unobserved   … gap_days を超えて観測が無い区間。破線
#   out_of_stock … 在庫切れが観測されている区間。点線
# `observed` キーは後方互換のため残す (= state == "observed")。
SEG_OBSERVED = "observed"
SEG_UNOBSERVED = "unobserved"
SEG_OUT_OF_STOCK = "out_of_stock"


def empty_spark(geom: SparkGeom) -> dict[str, Any]:
    return {"width": geom.width, "height": geom.height, "segments": [], "dots": [],
            "y_labels": [], "x_labels": [], "legend": None, "legends": [],
            "area": "", "last_point": None, "has_out_of_stock": False}


def _format_axis_date(dt: datetime, style: str) -> str:
    """x 軸の日付ラベル。``%-m`` 等はプラットフォーム依存なので自前で組む。"""
    if style == "short":
        return f"{dt.month}/{dt.day}"
    return dt.strftime("%Y-%m-%d")


def _build_legends(geom: SparkGeom, segments: list[dict[str, Any]],
                   x_min: float) -> list[dict[str, Any]]:
    """凡例エントリを、実際に描かれた状態のぶんだけ横に並べて返す (#5130 項目2)。

    未観測 (破線) と在庫切れ (点線) は同じ図に同時に出うるので、単数の
    ``legend`` から複数エントリに広げた。存在しない状態の凡例は出さない
    (凡例に書いてあるのに図に無い、を避ける)。
    """
    if not geom.show_legend:
        return []
    present = {s["state"] for s in segments}
    out: list[dict[str, Any]] = []
    for state, text in ((SEG_UNOBSERVED, geom.legend_text),
                        (SEG_OUT_OF_STOCK, geom.legend_text_out_of_stock)):
        if state not in present or not text:
            continue
        dash_x1 = x_min + len(out) * geom.legend_advance
        dash_x2 = dash_x1 + geom.legend_dash_len
        out.append({
            "state": state,
            "dash_x1": dash_x1,
            "dash_x2": dash_x2,
            "y": geom.legend_y,
            "text_x": dash_x2 + geom.legend_text_gap,
            "text": text,
        })
    return out


def _legacy_legend(geom: SparkGeom, segments: list[dict[str, Any]],
                   x_min: float) -> Optional[dict[str, Any]]:
    """#5120 の単数 ``legend`` キー。未観測の凡例だけを従来の形で返す。

    ``legends`` へ移行済みのテンプレは見ないが、キーを消すと外部の呼び出し側が
    静かに凡例を失うので残す。在庫切れしか無い図では None (従来の
    「破線＝未観測期間」を出すと図に無い凡例になるため)。
    """
    for entry in _build_legends(geom, segments, x_min):
        if entry["state"] == SEG_UNOBSERVED:
            return {k: v for k, v in entry.items() if k != "state"}
    return None


def build_spark(points: list[dict[str, Any]], min_price: int, max_price: int,
                geom: SparkGeom,
                extend_to_dt: Optional[datetime] = None,
                out_of_stock: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """#5120: SVG スパークラインの座標・軸ラベル・観測ドット・凡例をテンプレの
    外 (Python) で計算する。テンプレは受け取った文字列/座標をそのまま描画する
    だけにして、算術をテンプレ側に持たせない。

    x を「経過日数 (date, 日単位)」でなく秒解像度の ``ts`` に比例させる必要が
    あるのは、週1巡回 + 変化即追記のマージ実データで 335/1219 (27%) のページが
    同一日に 2 点以上の観測を持つため。date だけでは同日内の順序・間隔を
    復元できず点が重なる。#5120 の実測では等間隔描画が本来の位置から最大
    209px (描画幅296px中) ずれていた。

    折れ線を直線補間でなく階段 (step-after: 前の点の価格を次の観測まで
    水平に保持し、観測が来た瞬間に垂直移動) で描くのは、jsonl が dedupe を
    通った「価格の変化点」ログであり、観測点間の連続的な変化は記録されて
    いないため。直線補間は記録にない変化を描いてしまう。

    観測間隔が ``geom.gap_days`` (14日) を超える区間は「未観測」として別
    セグメントに分け、破線・低不透明度で描く。ただしギャップ辺の垂直移動
    (新観測時点で実際に変わった価格) は未観測ではないので、水平ホールド部分
    だけを破線側に、垂直部分は次の観測済みセグメントの先頭に入れる。

    ``dots`` は実観測点のみ (step-after で挿入した水平保持の中間頂点には
    打たない)。``geom.show_dots`` が False (カード版) なら空。

    ``extend_to_dt``: 「最終確認日まで価格が変わっていないことが latest.json で
    確定している」と呼び出し側が判定した場合にだけ渡される。渡されたら x 軸
    ドメインの終端をこの日時まで延ばし、最後の観測点から x 右端まで実線で
    水平に延長する (未観測ではなく確定した継続なので 14日ギャップ判定の対象外)。

    ``out_of_stock`` (#5130 項目2): 価格が取れなかった観測 (``price: null`` +
    ``availability``) の ``ts`` のリスト。#5130 項目1 で jsonl に残るように
    なったもの。

    在庫切れを線種で区別しないと、階段線は「最後に価格が取れた日の値が在庫切れ
    期間もずっと続いた」と主張してしまう。これは #5120 で潰した「直線補間が記録に
    ない連続変化を主張する」のと同じ種類の誤りで、今度は**記録にない価格の継続**を
    主張している。実測 (2026-08-13) では日次レーンの 123/1,890 ASIN (6.5%) が価格を
    取れておらず、うち 69 記事で価格履歴ブロックが描画されていた。

    2 つの形がある (2026-08-17 時点の実データでは 54 ASIN 中 50 が後者):

      - 区間型 … 価格観測にはさまれた在庫切れ。その区間の**水平ホールド部分**を
        out_of_stock セグメントに分ける (gap 判定と同じ仕組み。両方に該当する
        ときは在庫切れを採る — より具体的で、実際に観測された事実だから)
      - 末尾型 … 最後の価格観測より後がずっと在庫切れ (= 今も在庫切れ)。最後の
        価格観測から最新の在庫切れ観測まで out_of_stock セグメントを伸ばし、
        x 軸ドメインの終端もそこまで延ばす (その日に観測したことは事実なので、
        軸を伸ばすこと自体は嘘にならない)

    末尾型で伸ばすのは ``segments`` だけで、面塗り (``area``) と最新点マーカー
    (``last_point``) は最後の**価格**観測点で止める。面塗りは「価格がこのあたりを
    推移した」を示す図形なので価格の無い区間に広げてはいけないし、マーカーは
    「今いくらか」を示すものなので在庫切れの右端に置くと読者を誤らせる。
    """
    x_min, x_max = geom.plot_x_min, geom.plot_x_max
    draw_w = x_max - x_min
    y_top, y_bottom = geom.plot_y_top, geom.plot_y_bottom
    draw_h = y_bottom - y_top

    parsed: list[tuple[datetime, int]] = []
    for pt in points:
        dt = parse_ts(pt["ts"])
        if dt is None:
            continue
        parsed.append((dt, pt["price"]))

    if not parsed:
        return empty_spark(geom)

    n = len(parsed)
    oldest_dt = parsed[0][0]
    newest_dt = parsed[-1][0]

    # 在庫切れ観測 (#5130 項目2)。最初の価格観測より前のものは描く場所が無いので捨てる。
    oos_dts: list[datetime] = []
    for pt in (out_of_stock or []):
        dt = parse_ts(pt["ts"]) if isinstance(pt.get("ts"), str) else None
        if dt is not None and dt > oldest_dt:
            oos_dts.append(dt)
    oos_dts.sort()
    # 末尾型: 最後の価格観測より後にある在庫切れ観測の最新。
    oos_tail_dt = next((d for d in reversed(oos_dts) if d > newest_dt), None)

    extend = extend_to_dt is not None and extend_to_dt > newest_dt
    # extend (価格が続いたことが確定) と末尾在庫切れは両立しない。呼び出し側は
    # 在庫切れ ASIN に extend_to_dt を渡さない (build_post の 3 条件) が、万一
    # 両方来たら在庫切れを優先する — 「確定した継続」の主張のほうが強いため。
    if oos_tail_dt is not None:
        extend = False
    domain_end_dt = oos_tail_dt or (extend_to_dt if extend else newest_dt)
    total_seconds = (domain_end_dt - oldest_dt).total_seconds()
    price_range = max_price - min_price

    def _round(value: float) -> float | int:
        """#5225: geom.coord_precision に従って丸める。0 なら int にして
        points 文字列から不要な ".0" を消す (ページ重量が直接減る)。"""
        rounded = round(value, geom.coord_precision)
        return int(rounded) if geom.coord_precision == 0 else rounded

    def _y(price: int) -> float:
        if price_range == 0:
            return _round((y_top + y_bottom) / 2)
        return _round(y_top + (1 - (price - min_price) / price_range) * draw_h)

    def _x(index: int, dt: datetime) -> float:
        # 合計スパンが 0 秒のときだけ等間隔に落とす (ゼロ割り防止のガード)。
        if total_seconds <= 0:
            return _round(x_min + (index / (n - 1) * draw_w if n > 1 else 0.0))
        return _round(x_min + (dt - oldest_dt).total_seconds() / total_seconds * draw_w)

    coords = [(_x(i, dt), _y(price)) for i, (dt, price) in enumerate(parsed)]
    dots = [{"x": x, "y": y} for x, y in coords] if geom.show_dots else []

    y_labels = []
    if geom.show_labels:
        if price_range == 0:
            y_labels = [{
                "x": geom.y_label_x,
                "y": round((y_top + y_bottom) / 2, 1),
                "text": f"¥{min_price:,}",
                "anchor": geom.y_label_anchor,
            }]
        else:
            y_labels = [
                {"x": geom.y_label_x, "y": geom.y_label_max_y,
                 "text": f"¥{max_price:,}", "anchor": geom.y_label_anchor},
                {"x": geom.y_label_x, "y": geom.y_label_min_y,
                 "text": f"¥{min_price:,}", "anchor": geom.y_label_anchor},
            ]

    x_labels = []
    if geom.show_labels:
        x_labels = [
            {"x": x_min, "y": geom.x_label_y,
             "text": _format_axis_date(oldest_dt, geom.date_style), "anchor": "start"},
            {"x": x_max, "y": geom.x_label_y,
             "text": _format_axis_date(domain_end_dt, geom.date_style), "anchor": "end"},
        ]

    def _append_coord(coords_list: list[tuple[float, float]], xy: tuple[float, float]) -> None:
        # #5120 追補: 丸め後の座標が直前と完全一致するなら追加しない。価格が
        # 変わらない辺では step 頂点と実観測点が同一座標になり、そのまま積むと
        # 冗長な重複頂点が大量発生する (3,979 ページ分の無駄なマークアップ)。
        if coords_list and coords_list[-1] == xy:
            return
        coords_list.append(xy)

    def _area(path: list[tuple[float, float]]) -> str:
        """折れ線の下を塗る polygon の points。線を y 下端まで落として閉じる。

        破線 (未観測) 区間も含めた連続パスで塗る。塗りは「価格がどのあたりを
        推移したか」の形を読ませるためのもので、観測の有無は線種側で示す。
        """
        if not geom.show_area or len(path) < 2:
            return ""
        pts = list(path)
        # 底辺も同じ精度で丸める (float のままだと "47.0" が出て points が伸びる)。
        bottom = _round(y_bottom)
        pts.append((pts[-1][0], bottom))
        pts.append((pts[0][0], bottom))
        return " ".join(f"{x},{y}" for x, y in pts)

    def _seg(coords_list: list[tuple[float, float]], state: str) -> dict[str, Any]:
        return {
            "points": " ".join(f"{x},{y}" for x, y in coords_list),
            "state": state,
            # 後方互換 (#5120 の bool)。既存テンプレの `not seg.observed` は
            # 未観測・在庫切れの両方で真になり、少なくとも実線にはならない。
            "observed": state == SEG_OBSERVED,
        }

    def _flush(segs: list[dict[str, Any]], coords_list: list[tuple[float, float]],
               state: str) -> None:
        if len(coords_list) < 2:
            return
        segs.append(_seg(coords_list, state))

    def _oos_tail_segment(segs: list[dict[str, Any]], from_xy: tuple[float, float]) -> None:
        """末尾在庫切れ: 最後の価格観測から x 右端まで点線で水平に伸ばす。"""
        tail: list[tuple[float, float]] = [from_xy]
        _append_coord(tail, (_round(x_max), from_xy[1]))
        _flush(segs, tail, SEG_OUT_OF_STOCK)

    if n < 2:
        cur_seg: list[tuple[float, float]] = [coords[0]]
        if extend:
            _append_coord(cur_seg, (_round(x_max), coords[0][1]))
        segments = [_seg(cur_seg, SEG_OBSERVED)] if len(cur_seg) >= 2 else [
            _seg([coords[0]], SEG_OBSERVED)]
        # 面塗りと最新点マーカーは価格観測点で止める (在庫切れ区間には広げない)。
        last = {"x": cur_seg[-1][0], "y": cur_seg[-1][1]} if geom.show_last_marker else None
        area = _area(cur_seg)
        if oos_tail_dt is not None:
            _oos_tail_segment(segments, coords[0])
        return {"width": geom.width, "height": geom.height, "segments": segments,
                "dots": dots, "y_labels": y_labels, "x_labels": x_labels, "legend": None,
                "legends": _build_legends(geom, segments, x_min),
                "area": area, "last_point": last,
                "has_out_of_stock": oos_tail_dt is not None}

    edges = []
    for i in range(1, n):
        prev_dt, cur_dt = parsed[i - 1][0], parsed[i][0]
        # 在庫切れ観測が 2 つの価格観測のあいだにあれば、その水平ホールドは
        # 「価格が続いた」ではなく「在庫切れだった」。gap 判定より優先する
        # (実際に観測された事実のほうが具体的なので)。
        if any(prev_dt < d <= cur_dt for d in oos_dts):
            hold_state = SEG_OUT_OF_STOCK
        elif (cur_dt - prev_dt).total_seconds() / 86400.0 > geom.gap_days:
            hold_state = SEG_UNOBSERVED
        else:
            hold_state = SEG_OBSERVED
        edges.append((coords[i - 1], coords[i], hold_state))

    segments: list[dict[str, Any]] = []
    # cur_seg は常に「観測済み」の累積。破線セグメントはギャップ辺ごとに単発で挟まれる。
    cur_seg = [coords[0]]
    # full_path は破線区間も含めた1本の連続パス。面塗り (_area) 用に別途ためる。
    full_path: list[tuple[float, float]] = [coords[0]]

    for prev_xy, cur_xy, hold_state in edges:
        step_xy = (cur_xy[0], prev_xy[1])  # 前の価格を次の x まで水平に保持 (step-after)
        _append_coord(full_path, step_xy)
        _append_coord(full_path, cur_xy)
        if hold_state == SEG_OBSERVED:
            _append_coord(cur_seg, step_xy)
            _append_coord(cur_seg, cur_xy)
        else:
            # 水平ホールド部分だけを別セグメントに分ける。ギャップ辺の垂直移動
            # (新観測時点で実際に変わった価格) は未観測でも在庫切れでもないので、
            # 次の観測済みセグメントの先頭に入れる。
            _flush(segments, cur_seg, SEG_OBSERVED)
            hold: list[tuple[float, float]] = [prev_xy]
            _append_coord(hold, step_xy)
            _flush(segments, hold, hold_state)
            cur_seg = [step_xy]
            _append_coord(cur_seg, cur_xy)

    if extend:
        _append_coord(cur_seg, (_round(x_max), cur_seg[-1][1]))
        _append_coord(full_path, (_round(x_max), full_path[-1][1]))

    _flush(segments, cur_seg, SEG_OBSERVED)

    # 面塗りと最新点マーカーは価格観測点で止める (在庫切れ区間には広げない)。
    area = _area(full_path)
    last = {"x": full_path[-1][0], "y": full_path[-1][1]} if geom.show_last_marker else None

    if oos_tail_dt is not None:
        _oos_tail_segment(segments, coords[-1])

    return {"width": geom.width, "height": geom.height, "segments": segments,
            "dots": dots, "y_labels": y_labels, "x_labels": x_labels,
            "legend": _legacy_legend(geom, segments, x_min),
            "legends": _build_legends(geom, segments, x_min),
            "area": area, "last_point": last,
            "has_out_of_stock": any(s["state"] == SEG_OUT_OF_STOCK for s in segments)}


def build_card_spark(history: list[tuple[datetime, int]]) -> Optional[dict[str, Any]]:
    """一覧カード (/price/ /deals/) 用のスパークラインを作る。描画に値しない
    履歴なら None を返す (呼び出し側は item に ``spark`` キーを付けない)。

    引数は ``build_price_dashboard.load_merged_history`` の戻り値そのまま
    (``list[tuple[datetime, int]]``、ts 昇順)。build_spark が期待する dict 列への
    変換をここ1箇所に閉じ込め、/price/ と /deals/ で採択条件がズレないようにする。
    """
    if len(history) < CARD_MIN_POINTS:
        return None
    prices = {price for _, price in history}
    if len(prices) < CARD_MIN_DISTINCT_PRICES:
        return None
    min_price, max_price = min(prices), max(prices)
    points = [{"ts": dt.isoformat(), "price": price} for dt, price in history]
    spark = build_spark(points, min_price, max_price, CARD_GEOM)
    # カードの文脈 (値下げ幅・現在価格の隣) で縦軸の範囲を示せるよう、
    # 描画には使わないが min/max を添える。
    spark["min_price"] = min_price
    spark["max_price"] = max_price
    return spark
