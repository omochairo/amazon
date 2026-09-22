"""article_format_log.py

#7954 — 記事型 (「どこで買える」新型 (#2686) / 従来型) と公式手順の有無を
ページ (ASIN) 単位で記録する追記型ログ (data/analytics/history/article_format.jsonl)。

なぜ要るか: #4964 の効果測定 (新型 vs 従来型の impression 比較) をするには、
「そのページはその日どの型だったか」が復元できる必要がある。build_post.py は
`stock_title (#2686): N page(s)` という **件数** をログに出すだけで、どのページが
いつどの型だったかは記録していなかった。加えて記事型は日によって変わりうる
(#7953) ため、「今日の型」で過去の impression を遡って仕分けるのは誤り。

書き方は #6791 の `census_url_states.jsonl` (append_census_history.py) と同じ
流儀: 状態が変わった行だけ書く遷移ログ + 月初の自己修復スナップショット。
任意時点の状態は「その月の snapshot 行 + それ以降の差分行」を辿れば復元できる。

1 行のスキーマ:
    {
        "date": "YYYY-MM-DD",       # ビルド日 (UTC)。記事本体の `date` フィールドではない
        "asin": "B0XXXXXXXX",
        "format": "stock" | "legacy",
        "official_howto": "steps" | "link" | "none",
        "redated": true | false,
        "snapshot": true,            # 月初の自己修復行にのみ立つ (通常行には無し)
    }

呼び出し元: build_post.py (`--format-log <path>` 指定時のみ書く。指定が無い
ローカル/テストビルドではファイルを一切変更しない)。
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any

FORMAT_LOG_FILENAME = "article_format.jsonl"

# build_post.py の _PRIMARY_RE / _winning_stems と同じ命名規約
# (``YYYY-MM-DD-<ASIN>``) を前提に、ファイル名から日付を取り出す。
_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(B0[A-Z0-9]{8})$")


def earliest_dates_by_asin(stems: list[str]) -> dict[str, str]:
    """``YYYY-MM-DD-<ASIN>`` 形式のファイル stem 群から、ASIN ごとの最古の日付を作る。

    同一 ASIN で複数の日付ファイルが (書き直し途中で) 共存している場合に、
    「この ASIN は今回のファイルより前から存在していたか」を安く判定するための
    材料。命名規約に合わない stem は無視する (エラーにしない)。
    """
    earliest: dict[str, str] = {}
    for stem in stems:
        m = _STEM_RE.match(stem)
        if not m:
            continue
        date_str, asin = m.group(1), m.group(2)
        if asin not in earliest or date_str < earliest[asin]:
            earliest[asin] = date_str
    return earliest


def load_asin_origin_pool(path: pathlib.Path | str) -> dict[str, str]:
    """``data/analytics/asin_origin.jsonl`` を {asin: pool} に読む。

    同一 ASIN が複数行 (別 run) に出現する場合は最後の行を採用する
    (append_census_history.load_asin_origin と同じ方針)。ファイル無し/壊れた
    行は無視して寛容に扱う。
    """
    mapping: dict[str, str] = {}
    p = pathlib.Path(path)
    if not p.exists():
        return mapping
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return mapping
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        asin = row.get("asin")
        if isinstance(asin, str) and asin:
            mapping[asin] = row.get("pool")
    return mapping


def compute_redated(
    asin: str,
    article_date: str | None,
    asin_origin_pool: str | None,
    earliest_filename_date: str | None,
) -> bool:
    """その ASIN が「今回の記事の date より前から存在していたか」を判定する。

    2 つの安価なシグナルの OR:
      1. asin_origin.jsonl の pool が "rewrite-queue"
         (= 新規発掘ではなく既存 URL の書き直し対象として投入された ASIN)
      2. data/articles/ に同じ ASIN のより古い日付のファイルが (まだ) 残っている
         (= 書き直しで新しい日付ファイルが増えたが、旧ファイルが未整理)

    どちらも単独では完全ではない (1 は書き直し完了後に asin_origin へ記録され
    続ける前提、2 は旧ファイルが削除されると消える) が、git 履歴を ASIN ごとに
    辿る重い処理をしなくて済む。両方を OR で見ることで、どちらか一方が欠けても
    もう一方が拾えるようにする。
    """
    if asin_origin_pool == "rewrite-queue":
        return True
    if earliest_filename_date and article_date:
        # article_date は "YYYY-MM-DD" のこともあれば ISO タイムスタンプ
        # ("YYYY-MM-DDTHH:MM:SS+09:00") のこともある。素の文字列比較だと
        # "2026-05-14" < "2026-05-14T10:00:00+09:00" が真になってしまう
        # (前者が後者の prefix であるだけで、時刻付きのほうが辞書順で大きい) ため、
        # 日付部分 (先頭 10 文字) だけを切り出して比較する。
        article_date_only = article_date[:10]
        if earliest_filename_date < article_date_only:
            return True
    return False


def load_last_states(log_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """article_format.jsonl を読んで ASIN ごとの最新状態を返す。

    ファイルは日付順 (ビルド実行順) に追記されるため、同一 ASIN の行が複数
    あれば最後に出現した行が最新状態。壊れた行/型不正は無視する
    (このレーンを fail させない)。ファイル無しは空 dict (初回扱い)。
    """
    states: dict[str, dict[str, Any]] = {}
    if not log_path.exists():
        return states
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return states
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        asin = row.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        states[asin] = {
            "format": row.get("format"),
            "official_howto": row.get("official_howto"),
            "redated": row.get("redated"),
        }
    return states


def is_first_of_month(log_path: pathlib.Path, target_date: str) -> bool:
    """target_date と同じ年月 (YYYY-MM) の行が history に無ければ True。

    月初の自己修復ポイント判定 (append_census_history.is_first_census_of_month
    と同じ方針)。ファイル無し/壊れた行は「月初」扱いにして通す
    (壊さない側に倒す)。
    """
    ym = target_date[:7]
    if not log_path.exists():
        return True
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        d = row.get("date")
        if isinstance(d, str) and d[:7] == ym:
            return False
    return True


def build_rows(
    current_states: dict[str, dict[str, Any]],
    previous_states: dict[str, dict[str, Any]],
    target_date: str,
    snapshot: bool,
) -> list[dict[str, Any]]:
    """今回のビルド結果と前回状態を比べ、追記する行のリストを作る。

    ``current_states``: {asin: {"format": ..., "official_howto": ..., "redated": ...}}

    ``snapshot=True`` のとき (月初) は current_states の全件を無条件で書き、
    行に ``snapshot: True`` を立てる。それ以外は
    (format, official_howto, redated) の組が前回と異なる ASIN だけ書く。

    census_url_states.jsonl と異なり「退場した ASIN」の行は出さない。
    census 側は「not-indexed から抜けた」という意味のある終端状態
    (indexed/dropped) を持つが、この記事型ログでは記事が今回ビルドに
    出てこないことは「型が変わった」ことを意味しないため。
    """
    if snapshot:
        return [
            {
                "date": target_date,
                "asin": asin,
                "format": st.get("format"),
                "official_howto": st.get("official_howto"),
                "redated": st.get("redated"),
                "snapshot": True,
            }
            for asin, st in sorted(current_states.items())
        ]

    rows: list[dict[str, Any]] = []
    for asin, st in sorted(current_states.items()):
        prev = previous_states.get(asin)
        if (
            prev is not None
            and prev.get("format") == st.get("format")
            and prev.get("official_howto") == st.get("official_howto")
            and prev.get("redated") == st.get("redated")
        ):
            continue
        rows.append({
            "date": target_date,
            "asin": asin,
            "format": st.get("format"),
            "official_howto": st.get("official_howto"),
            "redated": st.get("redated"),
        })
    return rows


def append_rows(log_path: pathlib.Path, rows: list[dict[str, Any]]) -> int:
    """rows を jsonl に追記する。空リストなら何もしない。"""
    if not rows:
        return 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return len(rows)
