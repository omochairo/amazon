#!/usr/bin/env python3
"""kw_ledger.py — omcha.jp / navi.omcha.jp 共通のキーワード台帳 (omcha-ops#97 P0/P1)。

## 何を1つにして、何を分けるか

**1つにするのは「器」だけ。** 語の外部属性 (検索ボリューム・SD・月別推移) は
サイトに依存しないので、2つのリポで別々に取ると API 呼び出しが二重になるうえ、
取得日がずれて**同じ語に2つの真実**ができる。

**採否のスコアは分ける。** omcha は自 GSC が厚く外部ボリュームが要るのは
「まだ順位が無い語」だけ。navi は自 GSC が使えず (母数が薄くボットまみれ)、
外部 + WP 需要が主で、さらに Amazon の在庫という供給ゲートが要る。
式を1本にするとどちらかの罠を踏むので、**本スクリプトはスコアを持たない。**

**共通化の最大の利得は host crowding。** omcha.jp と navi.omcha.jp は同一ホスト族
なので「この語をどちらが担当するか」は本来1か所で決める問題。`assign.jsonl` が
それを持ち、`report` が衝突を機械で見つける。

設計の正本は omcha-ops の `analysis/2026-09-07-keyword-ledger-design.md`。

## 置き場所

本スクリプトは public 側 (`omochairo/amazon`)、**データは private の omcha-ops**
(`data/keywords/`)。機密なのは出力であってコードではない (CLAUDE.md)。
既定パスはスクリプト位置ではなく**カレントディレクトリ相対**で、omcha-ops を
作業ディレクトリにして実行する。

    cd path/to/omcha-ops
    python path/to/amazon/scripts/kw_ledger.py stats

## 取得は Claude、記録はこのスクリプト

Ubersuggest MCP は Claude からしか呼べず、スクリプトからも CI からも叩けない。
したがって台帳の更新は「セッションの作業」であって cron ではない。

    1. todo  <keywords.txt>    まだ取っていない語を出す
    2. (Claude が keyword_overview を 12 語ずつ並列で叩く)
    3. merge <batch.json>      返り値を追記する
    4. todo が 0 になるまで繰り返す

`keyword_suggestions` (面) → `keyword_overview` (点) の順は変えない。節約のため
ではなく、自分で思いつかない語が出てくるから。`keyword_metrics` の
`search_difficulty` は月次クォータを食うので使わない。

## 1 日 100 レポートしか引けない (2026-09-07 実測)

`keyword_overview` も `match_keywords` も **同じ日次 100 レポート枠**を食う。
枠が尽きると両方が `HTTP 403 daily reports limit: 100` を返す。

    5,292 語 ÷ 100/日 = **53 日**

したがって取り直しは「セッションで一気に」ではなく**毎日 100 語ずつの点滴**になる。
`refetch-queue` が「今日の 100 語」を優先順に出すのはこのため。**枠の使い道を
決めることがこの台帳の主な仕事**であって、全件を測ることではない。

## external.jsonl は append-only

再取得は**上書きせず追記**する。最新は `fetched_at` が最大の行。当時の数字を消すと
半年後に「効かなかった」を検証できない。

## 未測定と SV=0 を混ぜない

`monthly_searches` が空配列なら**未測定** (2026-09-06 実測で確実な印)。そのとき
`seo_difficulty` は 4/12/17/20 が機械的に入るだけで意味を持たないので読まない。
`measured=False` の語を「需要が無い」と読むのが、この台帳で一番やりやすい誤り。

## 表記ゆれは norm で1つに畳む

`norm` は `build_demand_keywords.normalize_key` (NFKC + 小文字 + 空白完全除去)。
「箱根 子連れ」と「箱根子連れ」は別レコードで返ってくるので、**足すと二重に数える**。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import io
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_demand_keywords as bdk  # noqa: E402

normalize_key = bdk.normalize_key

DEFAULT_EXTERNAL = "data/keywords/external.jsonl"
DEFAULT_ASSIGN = "data/keywords/assign.jsonl"
DEFAULT_LOC_ID = 2392   # Japan。location_suggest で引いた実 ID 以外を入れない
DEFAULT_LANGUAGE = "ja"
# 採否を分けた語 (assign に載っている語) だけを再取得する間隔。全件リフレッシュは
# 台帳を汚すだけで見返りが無い。
DEFAULT_STALE_DAYS = 90
SITES = ("omcha", "navi")
ROLES = ("primary", "secondary", "avoid")


def _today() -> str:
    return datetime.date.today().isoformat()


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                print("skip malformed line %d in %s: %s" % (line_no, path, e),
                      file=sys.stderr)
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 台帳の読み


def latest(rows: list[dict]) -> dict[tuple, dict]:
    """(norm, loc_id, language) -> 最新の1行。

    append-only なので同じキーが複数回現れる。`fetched_at` が最大のものを採る。
    同着 (同じ日に取り直した) はファイルの後ろ = 追記順で新しい方。
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("norm"), r.get("loc_id"), r.get("language"))
        cur = out.get(key)
        if cur is None or (r.get("fetched_at") or "") >= (cur.get("fetched_at") or ""):
            out[key] = r
    return out


def measured(row: dict) -> bool:
    """未測定 (プレースホルダ) でないか。

    `measured` を明示的に持っている行はそれを信じる。持っていない古い行は
    monthly_searches の有無で見る。
    """
    if "measured" in row:
        return bool(row["measured"])
    return bool(row.get("monthly_searches"))


# --------------------------------------------------------------------------
# 取り込み (P0 移行 / P1 merge)


def months(monthly) -> list[dict]:
    """MCP の [{"period":"202507","search_volume":N}] を {"month":"2025-07"} に直す。

    period のままだと並べ替えと突き合わせのたびに解釈が要る。
    """
    out = []
    for m in monthly or []:
        p = str(m.get("period") or m.get("month") or "")
        digits = p.replace("-", "")
        if len(digits) == 6 and digits.isdigit():
            month = "%s-%s" % (digits[:4], digits[4:])
        else:
            month = p
        out.append({"month": month,
                    "volume": m.get("search_volume", m.get("volume"))})
    return out


def row_from_mcp(r: dict, loc_id: int, language: str, block: str,
                 fetched_at: str, source: str = "mcp:keyword_overview") -> dict:
    monthly = months(r.get("monthly_searches"))
    return {
        "keyword": r.get("keyword"),
        "norm": normalize_key(r.get("keyword")),
        "loc_id": loc_id,
        "language": language,
        "sv": r.get("search_volume"),
        "sd": r.get("seo_difficulty"),
        "cpc": r.get("cpc"),
        "competition": r.get("competition"),
        "paid_difficulty": r.get("paid_difficulty"),
        "search_intent": r.get("search_intent"),
        "monthly_searches": monthly,
        # 空配列 = 未測定。SV=0 と区別する唯一の印なので必ず立てる。
        "measured": bool(monthly),
        "source": source,
        "block": block,
        "fetched_at": fetched_at,
    }


def cmd_import_cache(args) -> int:
    """omcha-ops の data/ubersuggest/cache.jsonl を台帳へ移す (P0)。"""
    ext = pathlib.Path(args.external)
    have = latest(read_jsonl(ext))
    added = skipped = 0
    new_rows = []
    for r in read_jsonl(pathlib.Path(args.src)):
        loc_id = r.get("loc_id", DEFAULT_LOC_ID)
        language = r.get("language", DEFAULT_LANGUAGE)
        row = row_from_mcp(r, loc_id, language, r.get("block", ""),
                           r.get("fetched") or args.fetched_at or _today())
        key = (row["norm"], loc_id, language)
        if not row["norm"] or key in have:
            skipped += 1
            continue
        new_rows.append(row)
        have[key] = row
        added += 1
    append_jsonl(ext, new_rows)
    print("import-cache: added=%d skipped=%d" % (added, skipped))
    return 0


def cmd_import_navi(args) -> int:
    """navi の data/analytics/ubersuggest_demand.json を台帳へ移す (P0)。

    こちらは競合ドメインの CSV エクスポート由来で、**月別推移も取得日も無い**。
    したがって `measured` を MCP と同じ根拠では決められない。volume>0 の行だけ
    measured=True にし、全行に `measured_unknown` を立てて「判定していない」ことを
    残す。**後から MCP で取り直す対象**という意味であって、需要が無いという意味では
    ない (0 を「需要なし」と読むのがこの台帳で一番やりやすい誤り)。

    loc/lang も CSV には無い。競合は全て日本のおもちゃサイトなので 2392/ja と
    みなすが、`source` を見れば仮定だと分かるようにしておく。
    """
    ext = pathlib.Path(args.external)
    data = json.loads(io.open(args.src, encoding="utf-8").read())
    have = latest(read_jsonl(ext))
    fetched_at = (data.get("generated_at") or "")[:10] or _today()
    added = skipped = 0
    new_rows = []
    for r in data.get("keywords", []):
        keyword = r.get("raw_query") or r.get("query")
        norm = normalize_key(keyword)
        if not norm:
            continue
        key = (norm, DEFAULT_LOC_ID, DEFAULT_LANGUAGE)
        if key in have:
            skipped += 1
            continue
        volume = r.get("volume")
        row = {
            "keyword": keyword,
            "norm": norm,
            "loc_id": DEFAULT_LOC_ID,
            "language": DEFAULT_LANGUAGE,
            "sv": volume,
            "sd": r.get("seo_difficulty"),
            "monthly_searches": [],
            "measured": bool(volume),
            "measured_unknown": True,
            "source": "csv:competitor-export",
            "block": args.block,
            "fetched_at": fetched_at,
            "extra": {
                "sites": r.get("sites"),
                "competitor_position": r.get("competitor_position"),
                "suspect_volume": r.get("suspect_volume"),
            },
        }
        new_rows.append(row)
        have[key] = row
        added += 1
    append_jsonl(ext, new_rows)
    print("import-navi: added=%d skipped=%d (fetched_at=%s)"
          % (added, skipped, fetched_at))
    return 0


def read_keywords(path: pathlib.Path) -> list[tuple[str, str]]:
    """1行1語。`#` 始まりはブロック見出しで、直後の語に付く。"""
    out = []
    block = ""
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                block = s.lstrip("#").strip()
                continue
            out.append((s, block))
    return out


def cmd_todo(args) -> int:
    have = latest(read_jsonl(pathlib.Path(args.external)))
    stale_before = None
    if args.refresh_stale:
        stale_before = (datetime.date.today()
                        - datetime.timedelta(days=args.stale_days)).isoformat()
    seen = set()
    n = 0
    for kw, _block in read_keywords(pathlib.Path(args.src)):
        norm = normalize_key(kw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        row = have.get((norm, args.loc_id, args.language))
        if row is None:
            print(kw)
            n += 1
        elif stale_before and (row.get("fetched_at") or "") < stale_before:
            print(kw)
            n += 1
    print("# todo=%d / listed=%d" % (n, len(seen)), file=sys.stderr)
    return 0


def load_wp_owned(path: pathlib.Path, pos_max: float,
                  min_clicks: float) -> set[str]:
    """WP が既に取っている語 (norm)。ブリッジ (wp_demand.jsonl) を読む。

    omcha.jp と navi.omcha.jp は同一ホスト族なので、WP が上位で取っている語を
    navi で狙っても露出は増えず枠を食い合う。**その語に日次 100 枠を使うのは無駄**
    なので取り直しの対象から外す。判定は navi の rank guard と同じ AND 条件。
    """
    owned = set()
    if not path or not path.exists():
        return owned
    for r in read_jsonl(path):
        norm = r.get("norm") or normalize_key(r.get("query"))
        if not norm:
            continue
        pos = r.get("position")
        clicks = r.get("clicks")
        if isinstance(pos, (int, float)) and isinstance(clicks, (int, float)):
            if pos <= pos_max and clicks >= min_clicks:
                owned.add(norm)
    return owned


def load_keep_list(path: pathlib.Path) -> set[str]:
    """取り直しを絞り込むための語リスト (norm の集合)。

    navi の `data/demand_keywords.json` (`keywords[].keyword` と `source_queries`)
    と、1行1語のテキストの両方を受ける。

    **なぜ絞るか (2026-09-07 実測):** 台帳の未判定 5,292 語を CSV volume の降順で
    流すと 53 日かかる。一方 navi の需要パイプラインが実際に採っているのは 220 語で、
    台帳の未判定ぶんと重なるのは **158 語** = 2 日で終わる。
    **枠は「使う語」に割り当てる。**
    """
    keep = set()
    text = io.open(path, encoding="utf-8").read()
    if path.suffix == ".json":
        data = json.loads(text)
        for k in data.get("keywords", []):
            if isinstance(k, str):
                keep.add(normalize_key(k))
                continue
            keep.add(normalize_key(k.get("keyword")))
            for q in k.get("source_queries") or []:
                keep.add(normalize_key(q))
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keep.add(normalize_key(line))
    keep.discard("")
    return keep


def cmd_refetch_queue(args) -> int:
    """取り直しの「今日のぶん」を優先順に出す。

    1 日 100 レポートしか引けない (docstring 参照) ので、全件を機械的に流すと
    53 日かかる。順番そのものが成果を決めるため、根拠を stderr に必ず出す。

    優先順位:
      1. `suspect_volume` を外す — CSV の集計崩れ (実測: たまごっち 1,000,000)。
         測り直す価値ではなく、CSV 側の値が壊れている印
      2. WP が既に取っている語を外す — navi では使えないので枠の無駄
      3. `--keep-list` があれば、そこに載っている語だけに絞る (load_keep_list 参照)
      4. 残りを CSV volume の降順。**CSV の値は当てにならないから測り直すのだが、
         順番を決める材料は他に無い。**「大きいと言われている語から確かめる」という
         意味であって、値を信じているわけではない
    """
    cur = latest(read_jsonl(pathlib.Path(args.external)))
    owned = load_wp_owned(pathlib.Path(args.wp_demand) if args.wp_demand else None,
                          args.guard_pos_max, args.guard_min_clicks)
    keep = load_keep_list(pathlib.Path(args.keep_list)) if args.keep_list else None
    cand = []
    n_suspect = n_owned = n_offlist = 0
    for r in cur.values():
        if not r.get("measured_unknown"):
            continue
        if args.source and r.get("source") != args.source:
            continue
        if (r.get("extra") or {}).get("suspect_volume"):
            n_suspect += 1
            continue
        if r.get("norm") in owned:
            n_owned += 1
            continue
        if keep is not None and r.get("norm") not in keep:
            n_offlist += 1
            continue
        cand.append(r)
    cand.sort(key=lambda r: (-(r.get("sv") or 0), r.get("norm") or ""))
    for r in cand[:args.limit]:
        print(r.get("keyword"))
    print("# queue: 候補 %d / 出力 %d / 除外 suspect=%d wp既得=%d リスト外=%d (残り %d)"
          % (len(cand), min(args.limit, len(cand)), n_suspect, n_owned, n_offlist,
             max(0, len(cand) - args.limit)), file=sys.stderr)
    if not owned and args.wp_demand:
        print("# 注意: WP 既得の語が 0 件。%s を読めているか確認する"
              % args.wp_demand, file=sys.stderr)
    return 0


def cmd_merge(args) -> int:
    """batch.json = {"loc_id":2392,"language":"ja","block":"...","results":[<MCPの返り値>...]}

    既にある語は既定で skip する (同じ日に取り直しても情報が増えない)。
    `--refresh` で追記する — **上書きではない。** 当時の数字を残す。
    """
    batch = json.loads(io.open(args.src, encoding="utf-8").read())
    ext = pathlib.Path(args.external)
    have = latest(read_jsonl(ext))
    loc_id = batch.get("loc_id", DEFAULT_LOC_ID)
    language = batch.get("language", DEFAULT_LANGUAGE)
    block = batch.get("block", "")
    fetched_at = batch.get("fetched") or _today()
    added = skipped = refreshed = 0
    new_rows = []
    for r in batch.get("results", []):
        row = row_from_mcp(r, loc_id, language, block, fetched_at)
        if not row["norm"]:
            continue
        key = (row["norm"], loc_id, language)
        if key in have:
            if not args.refresh:
                skipped += 1
                continue
            refreshed += 1
        else:
            added += 1
        new_rows.append(row)
        have[key] = row
    append_jsonl(ext, new_rows)
    print("merge: added=%d refreshed=%d skipped=%d total=%d"
          % (added, refreshed, skipped, len(have)))
    return 0


# --------------------------------------------------------------------------
# 割り当て (host crowding)


def cmd_assign(args) -> int:
    """1語をどちらのサイトが担当するかを記録する。

    **これが2サイトで台帳を共有する理由。** omcha.jp と navi.omcha.jp は同一
    ホスト族なので、同じ語を両方で主軸にすると露出が増えるのではなく枠を食い合う。
    """
    norm = normalize_key(args.keyword)
    if not norm:
        raise SystemExit("keyword が空")
    row = {
        "norm": norm,
        "keyword": args.keyword,
        "site": args.site,
        "url": args.url,
        "role": args.role,
        "decided_at": args.decided_at or _today(),
        "issue": args.issue,
        "note": args.note,
    }
    append_jsonl(pathlib.Path(args.assign), [row])
    print("assign: %s %s %s (%s)"
          % (args.site, args.role, args.keyword, row["decided_at"]))
    return 0


def latest_assign(rows: list[dict]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("norm"), r.get("site"))
        cur = out.get(key)
        if cur is None or (r.get("decided_at") or "") >= (cur.get("decided_at") or ""):
            out[key] = r
    return out


# --------------------------------------------------------------------------
# レポート


def cmd_stats(args) -> int:
    rows = read_jsonl(pathlib.Path(args.external))
    cur = latest(rows)
    print("external.jsonl: %d 行 / %d 語 (norm × loc × lang)" % (len(rows), len(cur)))
    # 「測った結果」と「測っていない」と「判定していない」を混ぜて出さない。
    # CSV 由来 (measured_unknown) を measured に足すと、実際より測れているように見える。
    judged = [r for r in cur.values() if not r.get("measured_unknown")]
    n_measured = sum(1 for r in judged if measured(r))
    n_unknown = len(cur) - len(judged)
    print("  測定の有無を判定できた: %d 語 (measured=%d / 未測定=%d)"
          % (len(judged), n_measured, len(judged) - n_measured))
    print("  判定していない (CSV 由来・要 MCP 取り直し): %d 語" % n_unknown)
    print("  source:")
    for k, v in collections.Counter(
            r.get("source", "?") for r in cur.values()).most_common():
        print("    %-28s %5d" % (k, v))
    print("  block:")
    for k, v in collections.Counter(
            r.get("block", "") for r in cur.values()).most_common(12):
        print("    %-28s %5d" % (k or "(no block)", v))
    assigns = latest_assign(read_jsonl(pathlib.Path(args.assign)))
    if assigns:
        by_site = collections.Counter(k[1] for k in assigns)
        print("assign.jsonl: %d 件 (%s)"
              % (len(assigns),
                 " / ".join("%s=%d" % (s, n) for s, n in by_site.most_common())))
    return 0


def find_conflicts(assigns: dict[tuple, dict]) -> list[tuple[str, list[dict]]]:
    """両サイトが同じ norm を primary にしている = host crowding の衝突。"""
    by_norm: dict[str, list[dict]] = {}
    for (norm, _site), r in assigns.items():
        if r.get("role") == "primary":
            by_norm.setdefault(norm, []).append(r)
    return [(n, rs) for n, rs in sorted(by_norm.items()) if len(rs) > 1]


def find_stale(assigns: dict[tuple, dict], cur: dict[tuple, dict],
               stale_days: int) -> list[dict]:
    """assign に載っているのに外部データが古い / そもそも無い語。

    全件を鮮度管理するのは無駄なので、**採否を分けた語だけ**を見る。
    """
    limit = (datetime.date.today() - datetime.timedelta(days=stale_days)).isoformat()
    newest: dict[str, str] = {}
    for (norm, _loc, _lang), r in cur.items():
        f = r.get("fetched_at") or ""
        if f > newest.get(norm, ""):
            newest[norm] = f
    out = []
    for (norm, site), _a in sorted(assigns.items()):
        f = newest.get(norm)
        if f is None:
            out.append({"norm": norm, "site": site, "reason": "台帳に無い",
                        "fetched_at": None})
        elif f < limit:
            out.append({"norm": norm, "site": site, "reason": "%dd 超" % stale_days,
                        "fetched_at": f})
    return out


def cmd_report(args) -> int:
    cur = latest(read_jsonl(pathlib.Path(args.external)))
    assigns = latest_assign(read_jsonl(pathlib.Path(args.assign)))

    conflicts = find_conflicts(assigns)
    print("## host crowding の衝突 (両サイトが primary) — %d 件" % len(conflicts))
    for norm, rs in conflicts:
        print("  %s" % norm)
        for r in rs:
            print("    %-6s %s" % (r.get("site"), r.get("url") or "-"))
    if not conflicts:
        print("  (無し)")

    stale = find_stale(assigns, cur, args.stale_days)
    print("\n## 採否を分けた語のうち外部データが古い — %d 件" % len(stale))
    for s in stale:
        print("  %-24s %-6s %s (%s)"
              % (s["norm"], s["site"], s["reason"], s["fetched_at"] or "-"))
    if not stale:
        print("  (無し)")

    unmeasured = [r for r in cur.values()
                  if not measured(r) and not r.get("measured_unknown")]
    print("\n## 未測定 (monthly_searches が空) — %d 語" % len(unmeasured))
    print("  **需要が無いという意味ではない。** 言い換え (裸の商品名・別表記・かな/カナ)")
    print("  を次のバッチに入れて測り直す。掛け合わせを増やすほどここに落ちる。")
    for r in sorted(unmeasured, key=lambda r: r.get("keyword") or "")[:args.limit]:
        print("    %s" % r.get("keyword"))
    if len(unmeasured) > args.limit:
        print("    ... 他 %d 語" % (len(unmeasured) - args.limit))
    return 0


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="キーワード台帳 (omcha-ops#97)")
    ap.add_argument("--external", default=DEFAULT_EXTERNAL)
    ap.add_argument("--assign", default=DEFAULT_ASSIGN)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("todo", help="まだ取っていない語を出す")
    p.add_argument("src", help="1行1語のテキスト (## がブロック見出し)")
    p.add_argument("--loc-id", type=int, default=DEFAULT_LOC_ID)
    p.add_argument("--language", default=DEFAULT_LANGUAGE)
    p.add_argument("--refresh-stale", action="store_true",
                   help="取得済みでも --stale-days を超えていれば再取得の対象にする")
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    p.set_defaults(func=cmd_todo)

    p = sub.add_parser("merge", help="MCP の返り値を追記する")
    p.add_argument("src", help="batch.json")
    p.add_argument("--refresh", action="store_true",
                   help="既にある語も追記する (再取得。上書きではない)")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("refetch-queue",
                       help="取り直しの今日のぶんを優先順に出す (1日100レポート制限)")
    p.add_argument("--limit", type=int, default=100,
                   help="1 日の上限。既定 100 = Ubersuggest の日次レポート枠")
    p.add_argument("--source", default="csv:competitor-export",
                   help="対象の source。空文字で全件")
    p.add_argument("--wp-demand", default="../amazon-navi-brain/demand/wp_demand.jsonl",
                   help="WP 既得語の判定に使うブリッジ")
    p.add_argument("--keep-list", default=None,
                   help="この語リストに載っているものだけに絞る "
                        "(navi の data/demand_keywords.json か 1行1語のテキスト)")
    p.add_argument("--guard-pos-max", type=float, default=3.0)
    p.add_argument("--guard-min-clicks", type=float, default=100.0)
    p.set_defaults(func=cmd_refetch_queue)

    p = sub.add_parser("import-cache", help="omcha-ops の cache.jsonl を移行する")
    p.add_argument("--src", default="data/ubersuggest/cache.jsonl")
    p.add_argument("--fetched-at", default=None)
    p.set_defaults(func=cmd_import_cache)

    p = sub.add_parser("import-navi", help="navi の ubersuggest_demand.json を移行する")
    p.add_argument("--src", required=True)
    p.add_argument("--block", default="navi-competitor-2026-08")
    p.set_defaults(func=cmd_import_navi)

    p = sub.add_parser("assign", help="語をどちらのサイトが担当するか記録する")
    p.add_argument("keyword")
    p.add_argument("--site", required=True, choices=SITES)
    p.add_argument("--role", default="primary", choices=ROLES)
    p.add_argument("--url", default=None)
    p.add_argument("--issue", default=None)
    p.add_argument("--note", default=None)
    p.add_argument("--decided-at", default=None)
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("stats", help="件数と内訳")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("report", help="衝突・鮮度・未測定")
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
