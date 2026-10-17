"""check_third_party_links (navi-brain#117 段階 1) と、リンク切れを数えない判定 (段階 2)。"""

import datetime as dt
import json

import check_third_party_links as chk
import fetch_third_party_sources as ftp
import score_per_asin_info as sc
from source_pages import Page

TODAY = dt.date(2026, 10, 17)


def _write(base, asin, sources):
    d = base / asin
    d.mkdir(parents=True, exist_ok=True)
    (d / "third_party_sources.json").write_text(
        json.dumps({"asin": asin, "fetched_at": "2026-10-10T00:00:00+00:00", "sources": sources},
                   ensure_ascii=False), encoding="utf-8")


def _src(url, **kw):
    from urllib.parse import urlparse
    return {"url": url, "host": urlparse(url).netloc, "title": "t", **kw}


def test_collect_targets_skips_retail_search_and_fresh_and_puts_pool_first(tmp_path):
    _write(tmp_path, "B000000001", [
        _src("https://blog.example.jp/a"),
        _src("https://www.amazon.co.jp/dp/B000000001/"),               # 販売
        _src("https://www.google.com/search?q=x"),                    # 検索結果
        _src("https://fresh.example.com/b", link={"status": "ok", "checked_at": "2026-10-10"}),
        _src("https://old.example.com/c", link={"status": "ok", "checked_at": "2026-08-01"}),
    ])
    _write(tmp_path, "B000000002", [_src("https://pool.example.org/p")])
    t = chk.collect_targets(tmp_path, TODAY, 30, pool={"B000000002"})
    assert [x[3] for x in t] == [
        "https://pool.example.org/p",     # pick 母集合・未確認
        "https://blog.example.jp/a",      # 母集合外・未確認
        "https://old.example.com/c",      # 母集合外・期限切れ
    ]


def test_check_urls_spaces_same_host_and_returns_all():
    calls = []

    def fake(url, timeout):
        calls.append(url)
        return Page("dead" if url.endswith("404") else "ok", 404 if url.endswith("404") else 200)

    res = chk.check_urls(["https://a.jp/1", "https://a.jp/404", "https://b.jp/1", "https://a.jp/1"],
                         workers=4, host_interval=0, timeout=1, fetch=fake)
    assert sorted(calls) == ["https://a.jp/1", "https://a.jp/404", "https://b.jp/1"]  # 重複は 1 回
    assert res["https://a.jp/404"].status == "dead"


def test_apply_results_writes_link_without_body_and_skips_rewritten_rows(tmp_path):
    _write(tmp_path, "B000000001", [_src("https://a.jp/1"), _src("https://a.jp/2")])
    targets = [(0, "B000000001", 0, "https://a.jp/1"), (0, "B000000001", 1, "https://stale.jp/x")]
    results = {"https://a.jp/1": Page("dead", 404, text="本文"), "https://stale.jp/x": Page("ok", 200)}
    assert chk.apply_results(tmp_path, targets, results, TODAY) == 1
    data = json.loads((tmp_path / "B000000001" / "third_party_sources.json").read_text(encoding="utf-8"))
    assert data["sources"][0]["link"] == {"status": "dead", "checked_at": "2026-10-17", "http": 404}
    assert "link" not in data["sources"][1]  # URL が変わった行には書かない
    assert "本文" not in json.dumps(data, ensure_ascii=False)


def test_dead_candidates_are_not_counted_by_score_or_fetch_lane(tmp_path):
    srcs = [
        _src("https://blog.example.jp/a", link={"status": "dead", "checked_at": "2026-10-17", "http": 404}),
        _src("https://media.example.com/b", link={"status": "unknown", "checked_at": "2026-10-17", "http": 403}),
        _src("https://maker.example.co.jp/c"),
    ]
    _write(tmp_path, "B000000001", srcs)
    hosts = sc._candidate_hosts("B000000001", tmp_path)["third_party"]
    assert hosts == {"media.example.com", "maker.example.co.jp"}  # unknown と未確認は数える
    assert ftp._site_count(srcs) == 2
    assert sc.link_dead(srcs[0]) and not sc.link_dead(srcs[1]) and not sc.link_dead(srcs[2])


def test_dns_outage_is_not_recorded_as_dead():
    res = {f"https://h{i}.jp/": Page("dead", None) for i in range(15)}
    res.update({f"https://ok{i}.jp/": Page("ok", 200) for i in range(10)})
    res["https://gone.jp/"] = Page("dead", 404)
    assert chk.suppress_dns_outage(res) == 15
    assert all(p.status == "unknown" for u, p in res.items() if u.startswith("https://h"))
    assert res["https://gone.jp/"].status == "dead"  # HTTP 404 はそのまま


def test_a_few_dns_failures_stay_dead():
    res = {f"https://ok{i}.jp/": Page("ok", 200) for i in range(30)}
    res["https://nx.jp/"] = Page("dead", None)
    assert chk.suppress_dns_outage(res) == 0
    assert res["https://nx.jp/"].status == "dead"


def test_apply_results_matches_rows_by_url_after_reorder(tmp_path):
    _write(tmp_path, "B000000001", [_src("https://a.jp/2"), _src("https://a.jp/1")])
    targets = [(0, "B000000001", 0, "https://a.jp/1")]  # 読んだときは 0 行目だった
    chk.apply_results(tmp_path, targets, {"https://a.jp/1": Page("ok", 200)}, TODAY)
    data = json.loads((tmp_path / "B000000001" / "third_party_sources.json").read_text(encoding="utf-8"))
    assert "link" not in data["sources"][0]
    assert data["sources"][1]["link"]["status"] == "ok"


def test_refetch_carries_link_for_same_url():
    prev = {"sources": [_src("https://a.jp/1", link={"status": "dead", "checked_at": "2026-10-01", "http": 404})]}
    new = [_src("https://a.jp/1"), _src("https://b.jp/2")]
    ftp.carry_link(new, prev)
    assert new[0]["link"]["status"] == "dead"
    assert "link" not in new[1]


def test_main_dry_run_does_not_fetch(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "B000000001", [_src("https://blog.example.jp/a")])
    monkeypatch.setattr(chk, "check_urls", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert chk.main(["--base", str(tmp_path), "--asin", "B000000001", "--dry-run"]) == 0
    assert "this run: 1" in capsys.readouterr().out
