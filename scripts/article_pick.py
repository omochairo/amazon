#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""記事生成の候補選定 (03-invoke-jules.yml の pick-asin の Python 版, #9073)。

GitLab 側の ``invoke_jules_repoless.py`` が使う。03 の inline Python
(``.github/workflows/03-invoke-jules.yml`` の pick-asin step) と同じ規則で候補を並べ、
各 ASIN の出自 (#4964 の台帳用) を付けて返す:

  first_party_pool (omcha-ops#264, 1 run 最大 FIRST_PARTY_PICKS_PER_RUN 件)
  → ranking_pool (#810, shuffle)
  → rewrite_queue (#5490, 最大 REWRITE_PICKS_PER_RUN 件)
  → keyword 候補 (shuffle)

並べる前に、**全プールの候補**からジャンル不一致 (#2823) と blocklist 済みを外す
(#9155)。取得時のゲートは入口ごとに付けてきたが、新しい入口を足すたびに付け忘れた
(2026-07-16〜19 の ranking-sniper、09 月の first-party)。記事生成に渡す直前の
ここを唯一の関門にして、入口側のゲートは「早めに落とす」最適化として扱う。
判定不能 (snapshot 無し・root 無し) は従来どおり通す (fail-open)。
手動指定 (03 の workflow_dispatch asin) はここを通らない。

その後 ``score_per_asin_info.should_defer`` で素材ゼロ品と、非販売ソースが 2 件
揃っていない品を外す (#1600 / #9025 / #9199)。first-party も同じ基準で待たせるが、
上限で切る**前に**外す (待っている ASIN に枠を占有させない)。first-party は落とさず
待たせるだけで、34-third-party-sources が優先して集める。安全弁 (全候補 defer なら
defer を無効化) は first-party を数えずに判定し、first-party の待ちは解除しない。

03 の pick-asin はこのモジュールを呼ぶだけにした (#9155)。03 と GitLab 側で
規則が 2 重に書かれていた頃の名残で、``scripts/tests/test_article_pick.py`` は
03 の inline コードと repoless を同じ入力で実行して比べている。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import re
import sys

ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def _warn(msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}")
    else:
        print(f"warning: {msg}", file=sys.stderr)


def _valid_asins(values) -> list:
    return [a for a in values if isinstance(a, str) and ASIN_RE.match(a)]


def load_ranking_pool(path: str = "data/raw/ranking_pool.json") -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return _valid_asins(json.load(f).get("asins", []))
    except FileNotFoundError:
        return []


def load_first_party_pool(path: str = "data/raw/first_party_pool.json") -> list:
    """無い/壊れていれば空 (収集レーンが無い間はこれが常態)。"""
    try:
        with open(path, encoding="utf-8") as f:
            return _valid_asins(json.load(f).get("asins", []))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
        _warn(f"first_party_pool skipped: {e}")
        return []


def load_blocklist(path: str = "data/asin_blocklist.json") -> set:
    """``{"blocked": [{"asin": ...}]}`` の ASIN 集合 (fetch_amazon._load_asin_blocklist と同じ形式)。

    無い/壊れていれば空 (pick を止めない)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as e:  # ValueError: JSONDecodeError / UnicodeDecodeError
        _warn(f"asin blocklist skipped: {e}")
        return set()
    blocked = data.get("blocked", []) if isinstance(data, dict) else []
    return {e["asin"] for e in blocked
            if isinstance(e, dict) and isinstance(e.get("asin"), str)}


def _exclusion_filter(blocked, genre_flagged):
    """ASIN -> 外す理由 ("blocklist" / "genre") か None を返す関数を作る。

    判定は 1 ASIN 1 回だけ (ジャンル判定は per_asin の snapshot を読むので)。
    """
    cache = {}

    def reason(a):
        if a not in cache:
            r = None
            if a in blocked:
                r = "blocklist"
            elif genre_flagged is not None:
                try:
                    if genre_flagged(a):
                        r = "genre"
                except Exception:  # 判定の失敗で pick を止めない (fail-open)
                    r = None
            cache[a] = r
        return cache[a]

    return reason, cache


def select_candidates(items, existing, ranking_pool, first_party_all,
                      rewrite_candidates, seed, scorer=None,
                      first_party_cap: int = 2, rewrite_cap: int = 2,
                      has_amazon_item=None, genre_flagged=None, blocked=None) -> list:
    """候補を優先順に並べ、``{"asin", "pool", "source_keyword"}`` のリストで返す。

    items: amazon.json の items[]。existing: 除外する ASIN (既存記事・open PR/MR・lock)。
    rewrite_candidates: ``rewrite_queue.pending_rewrite_candidates`` の戻り値
    (取得に失敗したら None)。scorer: ``score_per_asin_info`` (None なら defer しない)。
    has_amazon_item: ASIN -> per_asin に商品データがあるか (None なら判定しない)。
    genre_flagged: ASIN -> ジャンル不一致か (None なら判定しない)。
    blocked: blocklist 済み ASIN の集合 (None なら判定しない)。
    genre_flagged / blocked は全プールに効く (#9155)。
    pool が空文字の行は出自不明 (台帳に書かない)。
    """
    excluded_reason, excluded_seen = _exclusion_filter(blocked or set(), genre_flagged)
    candidates = [i["asin"] for i in items
                  if isinstance(i, dict) and isinstance(i.get("asin"), str)
                  and ASIN_RE.match(i["asin"])]

    fp_remaining = [a for a in first_party_all if a not in existing]
    # #9155: 商品データの無い first-party は取得されるまで待たせる (03 と同じ)。
    # 上限で切る前に外す: 後で外すとデータの無い ASIN が枠を占有し続ける。
    if has_amazon_item is not None:
        fp_waiting = [a for a in fp_remaining if not has_amazon_item(a)]
        if fp_waiting:
            print(f"first-party waiting for amazon data (#9155): {len(fp_waiting)} "
                  f"{fp_waiting[:10]}")
        fp_remaining = [a for a in fp_remaining if a not in set(fp_waiting)]
    # #9155: ジャンル不一致・blocklist 済みは上限で切る**前に**外す (枠を占有させない)。
    fp_remaining = [a for a in fp_remaining if excluded_reason(a) is None]
    # #9199: 非販売ソースが 2 件揃うまで待たせる (sources_v5 で確実に落ちるため)。
    # 落とさない: 34-third-party-sources が first-party を先頭で集める。
    if scorer is not None:
        try:
            fp_scores = {a: scorer.score_asin(a) for a in fp_remaining}
            fp_src_waiting = [a for a in fp_remaining if scorer.should_defer(fp_scores[a])]
            if fp_src_waiting:
                print(f"first-party waiting for third-party sources (#9199): "
                      f"{len(fp_src_waiting)} {fp_src_waiting[:10]}")
                exhausted = getattr(scorer, "sources_exhausted", None)
                stuck = [a for a in fp_src_waiting
                         if exhausted is not None and exhausted(fp_scores[a])]
                if stuck:
                    # 収集を試しても揃わなかった。待ち続けて記事にならないので人が見る。
                    _warn(f"first-party sources exhausted (#9199): {len(stuck)} "
                          f"{stuck[:10]} — 収集済みでも非販売ソース 2 件に届かない")
                fp_remaining = [a for a in fp_remaining if a not in set(fp_src_waiting)]
        except Exception as e:  # スコア計算失敗は pick を止めない (従来どおり通す)
            _warn(f"first-party source check skipped: {e}")
    first_party_pool = fp_remaining[:max(first_party_cap, 0)]
    if first_party_pool:
        print(f"first-party-ready (omcha-ops#264): {len(fp_remaining)} remaining "
              f"(pool {len(first_party_all)}) -> picking {len(first_party_pool)} {first_party_pool}")
    first_party_set = set(first_party_pool)
    ranking_set = set(ranking_pool)
    remaining_ranking = [a for a in ranking_pool
                         if a not in existing and a not in first_party_set
                         and excluded_reason(a) is None]
    remaining_kw = [a for a in candidates
                    if a not in existing and a not in ranking_set and a not in first_party_set
                    and excluded_reason(a) is None]

    rewrite_first = []
    if rewrite_candidates is not None:
        pool = [a for a in rewrite_candidates if a not in existing and a not in ranking_set
                and excluded_reason(a) is None]
        rewrite_first = pool[:max(rewrite_cap, 0)]
        if pool:
            print(f"rewrite-ready (#5490): {len(pool)} -> picking {len(rewrite_first)} "
                  f"{rewrite_first}")

    for why in ("genre", "blocklist"):
        hit = sorted(a for a, r in excluded_seen.items() if r == why)
        if hit:
            print(f"excluded at pick, {why} (#9155): {len(hit)} {hit[:10]}")

    rng = random.Random(seed)
    rng.shuffle(remaining_ranking)
    rng.shuffle(remaining_kw)
    remaining = first_party_pool + remaining_ranking + rewrite_first + [
        a for a in remaining_kw if a not in rewrite_first
    ]

    if scorer is not None:
        try:
            kept, deferred, kept_other = [], [], []
            for a in remaining:
                if a in first_party_set:
                    kept.append(a)
                    continue
                if scorer.should_defer(scorer.score_asin(a)):
                    deferred.append(a)
                else:
                    kept.append(a)
                    kept_other.append(a)
            if deferred:
                print(f"info-zero/awaiting-sources deferred (#1600/#9199): {len(deferred)} "
                      f"e.g. {deferred[:10]}")
            if kept_other:
                remaining = kept
            elif deferred:
                _warn("all candidates deferred (zero/unfetched); "
                      "proceeding without defer to avoid pool starvation")
        except Exception as e:  # スコア計算失敗は pick を止めない (best-effort)
            _warn(f"info scoring skipped, no defer applied: {e}")

    print(f"candidates={len(candidates)} ranking_pool={len(ranking_pool)} "
          f"first_party_pool={len(first_party_pool)} "
          f"existing_excluded={len(existing)} ranking_first={len(remaining_ranking)} "
          f"remaining={len(remaining)}")

    rewrite_set = set(rewrite_first)
    asin_meta = {i["asin"]: i for i in items
                 if isinstance(i, dict) and isinstance(i.get("asin"), str)}

    def origin_for(a):
        if a in first_party_set:
            return {"asin": a, "pool": "first-party", "source_keyword": None}
        if a in ranking_set:
            return {"asin": a, "pool": "ranking-sniper", "source_keyword": None}
        if a in rewrite_set:
            return {"asin": a, "pool": "rewrite-queue", "source_keyword": None}
        meta = asin_meta.get(a) or {}
        lane = meta.get("lane")
        if lane in ("demand", "supply-random"):
            return {"asin": a, "pool": lane, "source_keyword": meta.get("source_keyword")}
        return {"asin": a, "pool": "", "source_keyword": None}

    return [origin_for(a) for a in remaining]


# #9239: マージされずに close された記事 PR の ASIN を、しばらく pick に戻さない。
# close すると ASIN は候補に戻り、同じ素材のまま次の pick で再生成されていた
# (B0FYCTJF5Z は #9098 → #9165 → #9225 と 3 日で 3 回、同じ sources_v5 で close)。
# close 理由は問わない: マージされなかった PR を同じ入力で作り直しても同じ結果になる。
FAILED_PR_COOLDOWN_DAYS = 14
# 窓の中でこの回数以上 close された ASIN は、素材を取り直すまで戻さず人に回す。
FAILED_PR_REPEAT_WINDOW_DAYS = 30
FAILED_PR_REPEAT_LIMIT = 2


def _parse_ts(value):
    try:
        ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def recently_failed(closed_prs, now, material_updated=None):
    """マージされずに close された記事 PR から、pick に戻さない ASIN を返す (#9239)。

    closed_prs: ``gh pr list --state closed --search is:unmerged --json title,closedAt``
    の行。ASIN はタイトルから拾う (03 が open PR を除外するのと同じ)。
    material_updated: ASIN -> 事前収集を最後に取り直した時刻 (無ければ None)。
    最後の close より後に取り直していれば、入力が変わったので回数に関わらず戻す
    (B0C6THPT89 は 2 回とも素材の収集前に close され、その後の収集で揃った)。

    戻り値: (cooling, stuck)。cooling は最後の close から FAILED_PR_COOLDOWN_DAYS 日
    待たせる ASIN、stuck は窓の中で FAILED_PR_REPEAT_LIMIT 回以上 close され、その後も
    素材が変わっていない ASIN (待っても同じ結果になる見込みが高いので、人が判断する)。
    """
    window = now - _dt.timedelta(days=FAILED_PR_REPEAT_WINDOW_DAYS)
    closes: dict = {}
    for pr in closed_prs or []:
        if not isinstance(pr, dict):
            continue
        ts = _parse_ts(pr.get("closedAt"))
        if ts is None or ts < window:
            continue
        for a in set(re.findall(r"B0[A-Z0-9]{8}", str(pr.get("title") or "").upper())):
            closes.setdefault(a, []).append(ts)
    cooling, stuck = set(), set()
    for a, times in closes.items():
        last = max(times)
        updated = material_updated(a) if material_updated else None
        if updated is not None and updated > last:
            continue  # close の後に素材を取り直した: 入力が変わったので試し直す
        if len(times) >= FAILED_PR_REPEAT_LIMIT:
            stuck.add(a)
        elif now - last < _dt.timedelta(days=FAILED_PR_COOLDOWN_DAYS):
            cooling.add(a)
    return cooling, stuck


def _third_party_fetched_at(asin):
    try:
        with open(f"data/raw/per_asin/{asin}/third_party_sources.json", encoding="utf-8") as f:
            return _parse_ts(json.load(f).get("fetched_at"))
    except (OSError, ValueError, AttributeError):
        return None


def exclude_recently_failed(existing, path=None, now=None) -> set:
    """close 直後の ASIN を除外に足した集合を返す (#9239)。

    path は 03 が ``gh pr list`` で書く JSON (既定は環境変数 CLOSED_ARTICLE_PRS)。
    指定が無い・読めない入口 (GitLab 側など) は従来どおり除外しない (fail-open)。
    """
    path = path if path is not None else os.environ.get("CLOSED_ARTICLE_PRS", "")
    if not path:
        return set(existing)
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError) as e:
        _warn(f"closed article PR cooldown skipped (#9239): {e}")
        return set(existing)
    cooling, stuck = recently_failed(rows, now or _dt.datetime.now(_dt.timezone.utc),
                                     _third_party_fetched_at)
    cooling -= set(existing)
    stuck -= set(existing)
    if cooling:
        print(f"cooldown after unmerged close (#9239): {len(cooling)} {sorted(cooling)[:10]}")
    if stuck:
        _warn(f"repeatedly closed without merge (#9239): {len(stuck)} {sorted(stuck)[:10]} "
              f"— {FAILED_PR_REPEAT_WINDOW_DAYS} 日で {FAILED_PR_REPEAT_LIMIT} 回以上 close。"
              f"pick から外している。素材を足すか blocklist に入れるかは人が判断する")
    return set(existing) | cooling | stuck


def pick_from_repo(existing, seed) -> list:
    """作業ツリーのデータ (cwd = リポジトリ直下) を読んで ``select_candidates`` を呼ぶ。"""
    existing = exclude_recently_failed(existing)
    with open("data/raw/amazon.json", encoding="utf-8") as f:
        items = json.load(f).get("items", [])
    try:
        import rewrite_queue as rq
        rewrite_candidates = rq.pending_rewrite_candidates("data/articles")
    except Exception as e:
        _warn(f"rewrite candidate injection skipped: {e}")
        rewrite_candidates = None
    try:
        import score_per_asin_info as scorer
    except Exception as e:
        _warn(f"info scoring skipped, no defer applied: {e}")
        scorer = None
    try:
        import product_image

        def has_amazon_item(a):
            return product_image.has_amazon_item("data/raw/per_asin", a)
    except Exception as e:  # 判定できなければ従来どおり (pick を止めない)
        _warn(f"first-party amazon data check skipped: {e}")
        has_amazon_item = None
    try:
        import genre_gate

        def genre_flagged(a):
            return genre_gate.is_flagged_snapshot("data/raw/per_asin", a)
    except Exception as e:  # 判定できなければ従来どおり (pick を止めない)
        _warn(f"genre check skipped: {e}")
        genre_flagged = None
    return select_candidates(
        items, existing, load_ranking_pool(), load_first_party_pool(),
        rewrite_candidates, seed, scorer,
        first_party_cap=int(os.environ.get("FIRST_PARTY_PICKS_PER_RUN", "2")),
        rewrite_cap=int(os.environ.get("REWRITE_PICKS_PER_RUN", "2")),
        has_amazon_item=has_amazon_item,
        genre_flagged=genre_flagged,
        blocked=load_blocklist(),
    )
