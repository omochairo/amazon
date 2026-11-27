"""まとめの予定表 (navi-brain#218 C1〜C3) のテスト。

- C1 予定表の読み方: 壊れた行は飛ばす・exclude で外す・同じ ASIN は 1 本と数える
- C2 検索の枠: 目標に届いた予定・期限切れは使わない・急ぐ予定から・予定をまたいで交互に・回ごとにずらす
- C3 候補の点と上限: 当たる商品に点 (急ぐなら高く)・上限を超えた分は捨てずに後ろへ・出自に予定の key
- 本物の data/cluster_plan.yaml が読めて、検索語と正規表現がそろっている
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import article_pick  # noqa: E402
import candidate_priority  # noqa: E402
import cluster_plan as cp  # noqa: E402
import fetch_amazon  # noqa: E402
import record_asin_origin  # noqa: E402

TODAY = dt.date(2026, 11, 27)

PLAN = """
clusters:
  - key: indoor
    label: 室内遊具
    publish_by: 2027-03-01
    match: "滑り台|トランポリン"
    exclude: "シルバニア"
    search: [室内 滑り台, 子供 トランポリン, ジャングルジム 室内]
    target_articles: 2
  - key: water
    label: 水遊び
    publish_by: 2027-06-01
    match: "水鉄砲|プール"
    exclude: "ボールプール"
    search: [水鉄砲 子供, ビニールプール]
    target_articles: 5
  - key: old
    label: 終わった予定
    until: 2026-10-01
    match: "けん玉"
    search: [けん玉]
    target_articles: 10
  - key: broken
    match: "(("
  - label: key が無い
    match: "x"
"""


def _plan(tmp_path, text=PLAN):
    p = tmp_path / "plan.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _article(d, asin, name, tags=()):
    (d / f"2026-11-01-{asin}.json").write_text(
        json.dumps({"product": {"asin": asin, "name": name}, "tags": list(tags)}, ensure_ascii=False),
        encoding="utf-8")


def test_load_skips_broken_rows(tmp_path):
    clusters = cp.load(_plan(tmp_path))
    assert [c.key for c in clusters] == ["indoor", "water", "old"]
    assert cp.load(tmp_path / "missing.yaml") == []
    assert [c.key for c in cp.active(clusters, TODAY)] == ["indoor", "water"]


def test_match_and_exclude(tmp_path):
    clusters = cp.load(_plan(tmp_path))
    assert cp.match_item({"title": "ボーネルンド ファン・ジャンパー トランポリン"}, clusters, TODAY).key == "indoor"
    assert cp.match_item({"title": "シルバニアファミリー 滑り台セット"}, clusters, TODAY) is None
    assert cp.match_item({"title": "カラーボール ボールプール用"}, clusters, TODAY) is None
    assert cp.match_item({"title": "けん玉 日本けん玉協会認定"}, clusters, TODAY) is None  # 期限切れ
    # 両方に当たるときは公開日が近いほう
    assert cp.match_item({"title": "滑り台つきプール"}, clusters, TODAY).key == "indoor"


def test_count_articles_dedupes_asin_and_reads_tags(tmp_path):
    clusters = cp.load(_plan(tmp_path))
    arts = tmp_path / "articles"
    arts.mkdir()
    _article(arts, "B000000001", "すべり台 クッション", ["滑り台"])
    (arts / "2026-11-02-B000000001.json").write_text(
        json.dumps({"product": {"asin": "B000000001", "name": "滑り台 (書き直し)"}}), encoding="utf-8")
    _article(arts, "B000000002", "ウォーターガン", ["水鉄砲"])
    (arts / "2026-11-01-B000000002.enrichment.json").write_text("{}", encoding="utf-8")
    assert cp.count_articles(clusters, arts) == {"indoor": 1, "water": 1, "old": 0}


def test_search_keywords_order_rotation_and_target(tmp_path):
    clusters = cp.load(_plan(tmp_path))
    # indoor は 120 日以内 (急ぐ)・water より先。予定をまたいで 1 語ずつ
    kws = cp.search_keywords(clusters, 4, TODAY, {"indoor": 0, "water": 0}, rotation=0)
    assert kws == ["室内 滑り台", "水鉄砲 子供", "子供 トランポリン", "ビニールプール"]
    # 回ごとにずれる
    assert cp.search_keywords(clusters, 1, TODAY, {"indoor": 0, "water": 0}, rotation=1) == ["子供 トランポリン"]
    # 目標に届いた予定は使わない。期限切れ (old) も使わない
    assert cp.search_keywords(clusters, 4, TODAY, {"indoor": 2, "water": 0}, rotation=0) == [
        "水鉄砲 子供", "ビニールプール"]
    assert cp.search_keywords(clusters, 0, TODAY, {}) == []


def test_rotation_advances_per_run():
    a = cp.rotation_of(dt.datetime(2026, 11, 27, 0, 5, tzinfo=dt.timezone.utc))
    b = cp.rotation_of(dt.datetime(2026, 11, 27, 9, 5, tzinfo=dt.timezone.utc))
    c = cp.rotation_of(dt.datetime(2026, 11, 28, 0, 5, tzinfo=dt.timezone.utc))
    assert (b - a, c - b) == (1, 1)


def test_fetch_amazon_lane_and_keywords(tmp_path):
    assert fetch_amazon.classify_keyword_lane("室内 滑り台", [], ["室内 滑り台"], ["室内 滑り台"]) == "cluster"
    assert fetch_amazon.classify_keyword_lane("室内 滑り台", ["室内 滑り台"], [], ["室内 滑り台"]) == "user"
    assert fetch_amazon.classify_keyword_lane("x", [], [], []) == "supply-random"
    arts = tmp_path / "articles"
    arts.mkdir()
    now = dt.datetime(2026, 11, 27, 0, 0, tzinfo=dt.timezone.utc)
    kws = fetch_amazon.load_cluster_keywords(str(_plan(tmp_path)), 2, str(arts), now=now)
    assert len(kws) == 2 and kws[0] in ("室内 滑り台", "子供 トランポリン", "ジャングルジム 室内")
    assert fetch_amazon.load_cluster_keywords(str(_plan(tmp_path)), 0, str(arts), now=now) == []
    assert fetch_amazon.load_cluster_keywords(str(tmp_path / "missing.yaml"), 3, str(arts), now=now) == []


def test_priority_points(tmp_path):
    clusters = cp.load(_plan(tmp_path))
    item = {"asin": "B0A0000001", "title": "子供用 トランポリン", "lane": "cluster"}
    _, detail, _ = candidate_priority.score_candidate(item, clusters=clusters, today=TODAY)
    assert detail["cluster"] == candidate_priority.WEIGHTS["cluster_urgent"]  # 3/1 まで 94 日
    _, detail, _ = candidate_priority.score_candidate(
        {"asin": "B0A0000002", "title": "水鉄砲"}, clusters=clusters, today=TODAY)
    assert detail["cluster"] == candidate_priority.WEIGHTS["cluster"]
    _, detail, _ = candidate_priority.score_candidate({"asin": "B0A0000003", "title": "パズル"},
                                                      clusters=clusters, today=TODAY)
    assert detail["cluster"] == 0
    _, detail, _ = candidate_priority.score_candidate(item)
    assert detail["cluster"] == 0


def test_pick_puts_cluster_first_with_cap(tmp_path, capsys):
    terms = tmp_path / "characters.yaml"
    terms.write_text("characters: {}\n", encoding="utf-8")
    items = [{"asin": f"B0A00000{i:02d}", "title": f"generic toy {i}", "lane": "supply-random"}
             for i in range(1, 5)]
    items += [{"asin": f"B0C00000{i:02d}", "title": f"室内 滑り台 {i}", "lane": "cluster",
               "source_keyword": "室内 滑り台"} for i in range(1, 4)]
    out = article_pick.select_candidates(
        items, set(), [], [], None, 3, character_terms_path=terms, recent_character_counts={},
        existing_class_counts={}, has_browse_nodes=False,
        cluster_cap=2, cluster_plan_path=_plan(tmp_path),
        now=dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc))
    order = [o["asin"] for o in out]
    assert set(order[:2]) <= {"B0C0000001", "B0C0000002", "B0C0000003"}
    # 3 件目のまとめの商品は捨てずに最後へ
    assert order[-1].startswith("B0C") and len(order) == 7
    first = out[0]
    assert first["pool"] == "cluster" and first["cluster"] == "indoor"
    assert first["source_keyword"] == "室内 滑り台"
    assert "cluster" not in next(o for o in out if o["asin"].startswith("B0A"))
    assert "cluster candidates (navi-brain#218): 3 kept=2 demoted=1" in capsys.readouterr().out


def test_origin_record_accepts_cluster():
    rec = record_asin_origin.make_record(ts="t", run_id="r", workflow="w", asin="B0C0000001",
                                         pool="cluster", source_keyword="室内 滑り台", cluster="indoor")
    assert rec["pool"] == "cluster" and rec["cluster"] == "indoor"
    rec = record_asin_origin.make_record(ts="t", run_id="r", workflow="w", asin="B0A0000001",
                                         pool="demand", source_keyword=None)
    assert "cluster" not in rec


def test_datetime_values_and_scalar_search(tmp_path):
    p = _plan(tmp_path, """
clusters:
  - key: a
    publish_by: 2027-01-10 00:00:00
    until: 2027-12-31T00:00:00Z
    match: "滑り台"
    search: 室内 滑り台
    target_articles: 3
""")
    [c] = cp.load(p)
    assert c.publish_by == dt.date(2027, 1, 10) and c.until == dt.date(2027, 12, 31)
    assert c.search == ()                       # 文字列は 1 文字ずつに分けず、使わない
    assert c.urgent(TODAY) and not c.expired(TODAY)
    assert cp.load(_plan(tmp_path, "clusters: 123\n")) == []
    (tmp_path / "bad.yaml").write_bytes(b"\xff\xfe\x00bad")
    assert cp.load(tmp_path / "bad.yaml") == []


def test_past_publish_date_does_not_outrank_urgent(tmp_path):
    p = _plan(tmp_path, """
clusters:
  - key: past
    publish_by: 2026-11-01
    match: "滑り台"
    search: [x]
    target_articles: 3
  - key: soon
    publish_by: 2026-12-10
    match: "滑り台"
    search: [y]
    target_articles: 3
""")
    clusters = cp.load(p)
    assert cp.match_item({"title": "滑り台"}, clusters, TODAY).key == "soon"
    assert cp.search_keywords(clusters, 1, TODAY, {}) == ["y"]


def test_writer_lane_origin_rows_carry_cluster(tmp_path):
    import writer_lane
    rows_in = [{"asin": "B0C0000001", "pool": "cluster", "source_keyword": "室内 滑り台", "cluster": "indoor"},
               {"asin": "B0A0000001", "pool": "demand", "source_keyword": "x", "cluster": None}]

    class Env:
        python = sys.executable
        calls = []

        def run(self, cmd, **kw):
            self.calls.append(cmd)
            raise RuntimeError("stop after writing the input")

    try:
        writer_lane.record_origin(Env(), rows_in, "1", tmp_path)
    except Exception:
        pass
    lines = [json.loads(x) for x in (tmp_path / "origin_input.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["cluster"] == "indoor" and "cluster" not in lines[1]
    # record_asin_origin.py --input がその行を受けて台帳に cluster を残す
    out = tmp_path / "origin.jsonl"
    import subprocess
    r = subprocess.run([sys.executable, str(pathlib.Path(record_asin_origin.__file__)), "--input",
                        str(tmp_path / "origin_input.jsonl"), "--workflow", "t", "--run-id", "r1", "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    recs = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["pool"] == "cluster" and recs[0]["cluster"] == "indoor"
    assert "cluster" not in recs[1]


def test_real_plan_loads():
    clusters = cp.load()
    assert len(clusters) >= 4
    for c in clusters:
        assert c.search and c.target_articles > 0, c.key
    assert len({c.key for c in clusters}) == len(clusters)
