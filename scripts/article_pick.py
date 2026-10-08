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

その後 ``score_per_asin_info.should_defer`` で素材ゼロ品を外す (#1600 / #9025)。
first-party は defer しない。安全弁 (全候補 defer なら defer を無効化) は
first-party を数えずに判定する。

03 側と挙動がずれないことは ``scripts/tests/test_article_pick.py`` が
03 の inline コードを同じ入力で実行して比べている。03 の pick-asin を直したら、
ここも同じように直すこと。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys

ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def _warn(msg: str) -> None:
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


def select_candidates(items, existing, ranking_pool, first_party_all,
                      rewrite_candidates, seed, scorer=None,
                      first_party_cap: int = 2, rewrite_cap: int = 2,
                      has_amazon_item=None, genre_flagged=None) -> list:
    """候補を優先順に並べ、``{"asin", "pool", "source_keyword"}`` のリストで返す。

    items: amazon.json の items[]。existing: 除外する ASIN (既存記事・open PR/MR・lock)。
    rewrite_candidates: ``rewrite_queue.pending_rewrite_candidates`` の戻り値
    (取得に失敗したら None)。scorer: ``score_per_asin_info`` (None なら defer しない)。
    has_amazon_item: ASIN -> per_asin に商品データがあるか (None なら判定しない)。
    genre_flagged: ASIN -> ジャンル不一致か (None なら判定しない)。first-party にだけ使う。
    pool が空文字の行は出自不明 (台帳に書かない)。
    """
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
    # #9155: first-party は取得時のジャンルゲート (#2823) を通らないので、ここで外す (03 と同じ)。
    if genre_flagged is not None:
        fp_offgenre = [a for a in fp_remaining if genre_flagged(a)]
        if fp_offgenre:
            print(f"first-party skipped, genre mismatch (#9155): {len(fp_offgenre)} "
                  f"{fp_offgenre[:10]}")
        fp_remaining = [a for a in fp_remaining if a not in set(fp_offgenre)]
    first_party_pool = fp_remaining[:max(first_party_cap, 0)]
    if first_party_pool:
        print(f"first-party-ready (omcha-ops#264): {len(fp_remaining)} remaining "
              f"(pool {len(first_party_all)}) -> picking {len(first_party_pool)} {first_party_pool}")
    first_party_set = set(first_party_pool)
    ranking_set = set(ranking_pool)
    remaining_ranking = [a for a in ranking_pool
                         if a not in existing and a not in first_party_set]
    remaining_kw = [a for a in candidates
                    if a not in existing and a not in ranking_set and a not in first_party_set]

    rewrite_first = []
    if rewrite_candidates is not None:
        pool = [a for a in rewrite_candidates if a not in existing and a not in ranking_set]
        rewrite_first = pool[:max(rewrite_cap, 0)]
        if pool:
            print(f"rewrite-ready (#5490): {len(pool)} -> picking {len(rewrite_first)} "
                  f"{rewrite_first}")

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
                print(f"info-zero/unfetched deferred (#1600): {len(deferred)} "
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


def pick_from_repo(existing, seed) -> list:
    """作業ツリーのデータ (cwd = リポジトリ直下) を読んで ``select_candidates`` を呼ぶ。"""
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
        _warn(f"first-party genre check skipped: {e}")
        genre_flagged = None
    return select_candidates(
        items, existing, load_ranking_pool(), load_first_party_pool(),
        rewrite_candidates, seed, scorer,
        first_party_cap=int(os.environ.get("FIRST_PARTY_PICKS_PER_RUN", "2")),
        rewrite_cap=int(os.environ.get("REWRITE_PICKS_PER_RUN", "2")),
        has_amazon_item=has_amazon_item,
        genre_flagged=genre_flagged,
    )
