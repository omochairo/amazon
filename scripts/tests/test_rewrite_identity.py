"""#165 T16: 書き直しで検索の評価を壊さない (rewrite_identity と build_post / select の配線)。

- 台帳: 追記・最も古い行を使う・2 回目の書き直しで初回の値が保たれる・
  旧 JSON が cleanup で消えた後も build が元の値を使う
- build: 台帳の無い記事の出力は変わらない・印を付けた時点の在庫型判定を保つ・
  title_variants があっても元の title が出る・aliases に旧 slug がすべて入る
- select: 表示が多いページを外す・gsc のファイルが無い/空でも落ちない
- Jules への注記: 書き直しだけ文面が替わる
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import frontmatter

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apply_rewrite_targets as art  # noqa: E402
import build_jules_prompt  # noqa: E402
import rewrite_identity as ri  # noqa: E402
import rewrite_queue as rq  # noqa: E402
import select_rewrite_targets as srt  # noqa: E402

SCRIPTS_DIR = ROOT
NEW = "B0TEST0001"   # 書き直していない記事
RW = "B0TEST0002"    # 8/13 より前に公開して書き直した記事
RW_STOCK = "B0TEST0003"  # 8/13 以降に公開して在庫型だった記事を書き直した


def _article(asin, slug, date, title, name, **extra):
    d = {
        "slug": slug,
        "date": date,
        "title": title,
        "meta_description": f"{name}の説明",
        # Keep the synthetic article shape accepted by build_post's full
        # rendering path (the template reads product.prices.{amazon,rakuten,yahoo}).
        "product": {"asin": asin, "brand": "Test", "name": name, "best_price": 3000,
                    "prices": {
                        "amazon": {"price": 3000, "url": f"https://example.com/{asin}", "availability": ""},
                        "rakuten": {"price": 0, "url": "", "is_search": True},
                        "yahoo": {"price": 0, "url": "", "is_search": True},
                    }},
        "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                      "daily_use": "", "safety_note": "", "closing": ""},
        "persona_fit": {},
        "faq": [],
        "keywords": [],
    }
    d.update(extra)
    return d


def _write(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _jsonl(path: pathlib.Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ---------------------------------------------------------------- 台帳


def test_record_appends_and_skips_same_slug(tmp_path):
    arts = tmp_path / "articles"
    _write(arts / f"2026-05-01-{RW}.json", _article(RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "元の題", "元の名前"))
    led = str(tmp_path / "identity.jsonl")
    assert ri.record(RW, f"2026-05-01-{RW}", str(arts), led, recorded_at="2026-11-01T00:00:00Z")
    # 同じ slug の印の付け直しでは足さない
    assert not ri.record(RW, f"2026-05-01-{RW}", str(arts), led)
    rows = [json.loads(x) for x in pathlib.Path(led).read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "asin": RW, "slug": f"2026-05-01-{RW}", "date": "2026-05-01T10:00:00+09:00",
        "title": "元の題", "product_name": "元の名前", "recorded_at": "2026-11-01T00:00:00Z",
        "has_review_signals": False, "has_target_age": False,
    }]


def test_record_uses_displayed_title_variant_and_skips_unreadable(tmp_path):
    arts = tmp_path / "articles"
    _write(arts / f"2026-05-01-{RW}.json", _article(
        RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "JSONの題", "元の名前",
        title_variants=[{"title": "表示していた題"}]))
    led = str(tmp_path / "identity.jsonl")
    assert ri.record(RW, f"2026-05-01-{RW}", str(arts), led)
    assert ri.load(led)[RW].title == "表示していた題"
    # 本体が無い・空の記事は足さない
    assert not ri.record(NEW, f"2026-05-01-{NEW}", str(arts), led)
    _write(arts / f"2026-05-02-{NEW}.json", {})
    assert not ri.record(NEW, f"2026-05-02-{NEW}", str(arts), led)


def test_second_rewrite_keeps_first_values(tmp_path):
    arts = tmp_path / "articles"
    _write(arts / f"2026-05-01-{RW}.json", _article(RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "元の題", "元の名前"))
    _write(arts / f"2026-11-10-{RW}.json", _article(RW, f"2026-11-10-{RW}", "2026-11-10T10:00:00+09:00", "1回目の題", "1回目の名前"))
    led = str(tmp_path / "identity.jsonl")
    assert ri.record(RW, f"2026-05-01-{RW}", str(arts), led, recorded_at="2026-11-01T00:00:00Z")
    assert ri.record(RW, f"2026-11-10-{RW}", str(arts), led, recorded_at="2026-12-01T00:00:00Z")
    ident = ri.load(led)[RW]
    assert (ident.title, ident.product_name) == ("元の題", "元の名前")
    assert ident.recorded_date == "2026-05-01T10:00:00+09:00"
    assert ident.first_date == "2026-05-01T10:00:00+09:00"
    assert ident.slugs == [f"2026-05-01-{RW}", f"2026-11-10-{RW}"]


def test_first_date_takes_older_rewrite_ledger_slug(tmp_path):
    led = tmp_path / "identity.jsonl"
    _jsonl(led, [{"asin": RW, "slug": f"2026-09-01-{RW}", "date": "2026-09-01T10:00:00+09:00",
                  "title": "t", "product_name": "n", "recorded_at": "2026-11-01T00:00:00Z"}])
    ledger = tmp_path / "rewrite_ledger.jsonl"
    _jsonl(ledger, [
        {"asin": RW, "old_slug": f"2026-05-20-{RW}", "new_slug": f"2026-09-01-{RW}"},
        {"asin": RW, "old_slug": f"2026-05-20-{RW}", "new_slug": f"2026-09-01-{RW}"},
        {"asin": NEW, "old_slug": "not-a-slug"},
    ])
    olds = ri.load_old_slugs(str(ledger))
    assert olds == {RW: [f"2026-05-20-{RW}"]}
    ident = ri.load(str(led), olds)[RW]
    assert ident.first_date == "2026-05-20T00:00:00+09:00"
    # 在庫型の判定は印を付けた時点の記事の date を使い、first_date は公開日用
    assert ident.recorded_date == "2026-09-01T10:00:00+09:00"


def test_load_uses_first_appended_row_even_if_timestamp_is_later(tmp_path):
    p = tmp_path / "identity.jsonl"
    _jsonl(p, [
        {"asin": RW, "slug": f"2026-05-01-{RW}", "date": "2026-05-01T10:00:00+09:00",
         "title": "初回", "product_name": "初回商品名", "recorded_at": "2026-12-01T00:00:00Z"},
        {"asin": RW, "slug": f"2026-11-10-{RW}", "date": "2026-11-10T10:00:00+09:00",
         "title": "2回目", "product_name": "2回目商品名", "recorded_at": "2026-11-01T00:00:00Z"},
    ])
    ident = ri.load(str(p))[RW]
    assert (ident.title, ident.product_name) == ("初回", "初回商品名")


def test_load_missing_or_broken_ledger(tmp_path):
    assert ri.load(str(tmp_path / "none.jsonl")) == {}
    p = tmp_path / "broken.jsonl"
    p.write_text('not json\n{"asin": "bad"}\n\n', encoding="utf-8")
    assert ri.load(str(p)) == {}


def test_ensure_pending_records_markers_without_rows(tmp_path):
    arts = tmp_path / "articles"
    q = tmp_path / "queue"
    _write(arts / f"2026-05-01-{RW}.json", _article(RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "元の題", "元の名前"))
    rq.write_marker(RW, f"2026-05-01-{RW}", str(q))
    rq.write_marker(NEW, "", str(q))  # 本体の無い印は記録しない
    led = str(tmp_path / "identity.jsonl")
    assert ri.ensure_pending(str(q), str(arts), led) == 1
    assert ri.ensure_pending(str(q), str(arts), led) == 0
    assert set(ri.load(led)) == {RW}


def test_old_slug_aliases_dedupes_and_skips_current():
    ident = ri.Identity(asin=RW, slugs=[f"2026-05-01-{RW}", f"2026-11-10-{RW}"])
    olds = {RW: [f"2026-04-01-{RW}", f"2026-05-01-{RW}"]}
    assert ri.old_slug_aliases(RW, f"2026-11-10-{RW}", olds, ident) == [
        f"/posts/2026-04-01-{RW.lower()}/", f"/posts/2026-05-01-{RW.lower()}/"]
    assert ri.old_slug_aliases(NEW, f"2026-05-01-{NEW}", olds, None) == []


def test_apply_rewrite_targets_main_records_identity(tmp_path, monkeypatch):
    pool = tmp_path / "amazon.json"
    _write(pool, {"items": []})
    per_asin = tmp_path / "per_asin"
    _write(per_asin / RW / "amazon.json", {"asin": RW, "item": {"asin": RW, "title": "x"}})
    arts = tmp_path / "articles"
    _write(arts / f"2026-05-01-{RW}.json", _article(RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "元の題", "元の名前"))
    led = tmp_path / "identity.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "apply_rewrite_targets.py", "--asins", RW, "--pool", str(pool),
        "--per-asin-root", str(per_asin), "--articles-dir", str(arts),
        "--queue-dir", str(tmp_path / "queue"), "--identity-path", str(led)])
    assert art.main() == 0
    assert ri.load(str(led))[RW].title == "元の題"


# ---------------------------------------------------------------- build


def _setup_site(tmp: pathlib.Path) -> None:
    (tmp / "data" / "raw" / "per_asin").mkdir(parents=True)
    (tmp / "data" / "raw" / "amazon.json").write_text('{"items": []}', encoding="utf-8")
    (tmp / "hugo").mkdir()
    (tmp / "hugo" / "config.toml").write_text(
        'baseURL = "https://example.com/"\n[params]\n  amazonPartnerTag = "chk01-22"\n', encoding="utf-8")
    shutil.copytree(SCRIPTS_DIR / "templates", tmp / "scripts" / "templates")
    # 在庫型の判定 (sticky) に使う在庫の履歴。8/13 以降に分類できる観測がある
    for asin in (NEW, RW, RW_STOCK):
        _jsonl(tmp / "data" / "price_watch" / "history" / f"{asin}.jsonl", [
            {"ts": "2026-11-01T00:00:00+00:00", "source": "amazon", "price": 3000, "availability": "在庫あり。"}])


def _build(tmp: pathlib.Path, src: pathlib.Path, dst: pathlib.Path, *extra: str) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    env = os.environ.copy()
    env.pop("GITHUB_STEP_SUMMARY", None)
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, str(SCRIPTS_DIR / "build_post.py"),
           "--src", str(src) + os.sep, "--dst", str(dst) + os.sep,
           "--raw-amazon", str(tmp / "data" / "raw" / "amazon.json"),
           "--per-asin-root", str(tmp / "data" / "raw" / "per_asin"), *extra]
    proc = subprocess.run(cmd, cwd=str(tmp), env=env, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Error processing" not in proc.stdout, proc.stdout


def _fixture(tmp: pathlib.Path):
    """旧本体に印を付け (台帳に記録) → Jules の新本体が着地 → cleanup で旧本体が消える、まで進める。"""
    _setup_site(tmp)
    src = tmp / "data" / "articles"
    q = tmp / "data" / "rewrite_queue"
    _write(src / f"2026-06-01-{NEW}.json", _article(NEW, f"2026-06-01-{NEW}", "2026-06-01T10:00:00+09:00", "書き直していない題", "そのままの名前"))
    _write(src / f"2026-05-01-{RW}.json", _article(RW, f"2026-05-01-{RW}", "2026-05-01T10:00:00+09:00", "元の題", "元の名前"))
    _write(src / f"2026-09-01-{RW_STOCK}.json", _article(RW_STOCK, f"2026-09-01-{RW_STOCK}", "2026-09-01T10:00:00+09:00", "在庫型だった記事の題", "在庫型の名前"))
    identity = tmp / "data" / "analytics" / "rewrite_identity.jsonl"
    art.write_markers([RW, RW_STOCK], str(src), str(q), str(identity))
    # Jules の新本体 (題・商品名が変わり、date が新しい。title_variants も付けてくる)
    _write(src / f"2026-11-10-{RW}.json", _article(
        RW, f"2026-11-10-{RW}", "2026-11-10T10:00:00+09:00", "Jules の新しい題", "Jules の新しい名前",
        title_variants=[{"title": "Jules の題の案"}], h1_recommendation="Jules の H1 案"))
    _write(src / f"2026-11-10-{RW_STOCK}.json", _article(
        RW_STOCK, f"2026-11-10-{RW_STOCK}", "2026-11-10T10:00:00+09:00", "Jules の題2", "Jules の名前2"))
    ledger = tmp / "data" / "analytics" / "rewrite_ledger.jsonl"
    # 過去にも 1 度書き直していた (その旧 slug の転送も残す)
    _jsonl(ledger, [{"asin": RW, "old_slug": f"2026-04-20-{RW}", "new_slug": f"2026-05-01-{RW}",
                     "completed_at": "2026-05-01T00:00:00Z", "source": "backfill"}])
    rq.cleanup_completed(str(src), str(q), str(ledger))
    assert not (src / f"2026-05-01-{RW}.json").exists()  # 旧 JSON は消えた
    return src, identity, ledger


def _post(dst: pathlib.Path, slug: str):
    return frontmatter.loads((dst / f"{slug}.md").read_text(encoding="utf-8"))


def test_build_uses_identity_after_cleanup(tmp_path):
    src, identity, ledger = _fixture(tmp_path)
    dst = tmp_path / "posts"
    _build(tmp_path, src, dst, "--rewrite-identity", str(identity), "--rewrite-ledger", str(ledger))
    post = _post(dst, f"2026-11-10-{RW}")
    # 題 (H1 / og:title) は元の title。title_variants・在庫型に差し替わらない
    assert post["title"] == "元の題"
    assert "どこで買える" not in post["title"]
    assert post["product_name"] == "元の名前"
    assert post["date"] == "2026-04-20T00:00:00+09:00"  # 初回の公開日 (rewrite_ledger の旧 slug)
    assert post.get("seo_title", "").startswith("元の名前") or "seo_title" not in post
    # H2 の商品名も元の名前
    h2 = [ln for ln in post.content.splitlines() if ln.startswith("## ")]
    assert any("元の名前" in ln for ln in h2)
    assert not any("Jules の新しい名前" in ln for ln in post.content.splitlines())
    webpage = json.loads(post["jsonld"]["webpage"])
    assert webpage["datePublished"] == "2026-04-20T00:00:00+09:00"
    assert "Jules" not in json.dumps(webpage, ensure_ascii=False)
    # 旧 slug の転送: rewrite_ledger の過去分すべて + 今回の旧 slug
    assert post["aliases"] == [f"/posts/2026-11-10-{RW.lower()}/",
                               f"/posts/2026-04-20-{RW.lower()}/",
                               f"/posts/2026-05-01-{RW.lower()}/"]
    # JSON 自体は書き換えない
    raw = json.loads((src / f"2026-11-10-{RW}.json").read_text(encoding="utf-8"))
    assert raw["title"] == "Jules の新しい題"
    assert raw["product"]["name"] == "Jules の新しい名前"


def test_build_without_identity_shows_the_regression(tmp_path):
    """対照: 台帳を読まなければ、同じ記事は Jules の題・名前で在庫型になる (= 守っているものの確認)。"""
    src, _identity, _ledger = _fixture(tmp_path)
    dst = tmp_path / "posts"
    _build(tmp_path, src, dst, "--rewrite-identity", str(tmp_path / "none.jsonl"),
           "--rewrite-ledger", str(tmp_path / "none2.jsonl"))
    post = _post(dst, f"2026-11-10-{RW}")
    assert "どこで買える" in post["title"]
    assert post["product_name"] == "Jules の新しい名前"
    assert post["aliases"] == [f"/posts/2026-11-10-{RW.lower()}/"]


def test_stock_format_kept_for_article_that_was_stock(tmp_path):
    src, identity, ledger = _fixture(tmp_path)
    dst = tmp_path / "posts"
    _build(tmp_path, src, dst, "--rewrite-identity", str(identity), "--rewrite-ledger", str(ledger))
    post = _post(dst, f"2026-11-10-{RW_STOCK}")
    assert post["title"].startswith("在庫型の名前はどこで買える")
    assert post["date"] == "2026-09-01T10:00:00+09:00"
    assert post["aliases"] == [f"/posts/2026-11-10-{RW_STOCK.lower()}/",
                               f"/posts/2026-09-01-{RW_STOCK.lower()}/"]


def test_stock_format_uses_recorded_date_when_first_slug_predates_rollout(tmp_path):
    src, identity, ledger = _fixture(tmp_path)
    # 初回 slug は 8/13 より前だが、印を付けた記事の date は 8/13 以降。
    rows = [json.loads(x) for x in identity.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["asin"] == RW_STOCK:
            row["date"] = "2026-09-01T10:00:00+09:00"
    _jsonl(identity, rows)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"asin": RW_STOCK, "old_slug": f"2026-08-01-{RW_STOCK}"}) + "\n")
    dst = tmp_path / "posts"
    _build(tmp_path, src, dst, "--rewrite-identity", str(identity), "--rewrite-ledger", str(ledger))
    post = _post(dst, f"2026-11-10-{RW_STOCK}")
    assert post["title"].startswith("在庫型の名前はどこで買える")
    assert post["date"] == "2026-08-01T00:00:00+09:00"


def test_recorded_seo_title_signals_are_preserved(tmp_path):
    src, identity, ledger = _fixture(tmp_path)
    rows = [json.loads(x) for x in identity.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["asin"] == RW:
            row["has_review_signals"] = True
            row["has_target_age"] = True
            row["title"] = "ロンビーの口コミや対象年齢と最安値を詳しく紹介する記事"
    _jsonl(identity, rows)
    current = src / f"2026-11-10-{RW}.json"
    article = json.loads(current.read_text(encoding="utf-8"))
    article["title"] = "ロンビーの口コミや対象年齢と最安値を詳しく紹介する記事"
    _write(current, article)
    dst = tmp_path / "posts"
    _build(tmp_path, src, dst, "--rewrite-identity", str(identity), "--rewrite-ledger", str(ledger))
    post = _post(dst, f"2026-11-10-{RW}")
    assert post["seo_title"].endswith("の口コミ・最安値・対象年齢")


def test_apply_updates_jsonld_product_name():
    data = {"product": {"name": "新商品名"}, "jsonld": {"product": {"name": "古い名前"}}}
    ri.apply(data, ri.Identity(asin=RW, product_name="元の商品名"))
    assert data["jsonld"]["product"]["name"] == "元の商品名"


def test_article_without_identity_is_byte_identical(tmp_path):
    src, identity, ledger = _fixture(tmp_path)
    out_with = tmp_path / "posts_with"
    out_without = tmp_path / "posts_without"
    _build(tmp_path, src, out_with, "--rewrite-identity", str(identity), "--rewrite-ledger", str(ledger))
    _build(tmp_path, src, out_without, "--rewrite-identity", str(tmp_path / "none.jsonl"),
           "--rewrite-ledger", str(tmp_path / "none2.jsonl"))
    name = f"2026-06-01-{NEW}.md"
    assert (out_with / name).read_bytes() == (out_without / name).read_bytes()


# ---------------------------------------------------------------- select


def _gsc_rows(asin, dates, imp):
    return [{"date": d, "page": f"https://navi.omcha.jp/products/{asin.lower()}/",
             "impressions": imp, "clicks": 0} for d in dates]


def test_page_impressions_window_is_last_28_days_of_data(tmp_path):
    p = tmp_path / "gsc.jsonl"
    rows = _gsc_rows(RW, ["2026-10-01", "2026-10-15", "2026-11-11"], 100)
    rows += _gsc_rows(NEW, ["2026-11-11"], 5)
    rows.append({"date": "2026-11-11", "page": "https://navi.omcha.jp/tags/x/", "impressions": 999})
    _jsonl(p, rows)
    # 窓は 2026-10-15〜2026-11-11 (データの最新日から 28 日)
    assert srt.page_impressions(str(p)) == {RW: 200, NEW: 5}


def test_split_protected_threshold_and_order():
    cands = [{"asin": a} for a in (NEW, RW, RW_STOCK)]
    kept, protected = srt.split_protected(cands, {RW: 200, RW_STOCK: 199})
    assert [c["asin"] for c in kept] == [NEW, RW_STOCK]
    assert protected == [RW]
    assert srt.PROTECT_MIN_IMPRESSIONS == 200


def test_page_impressions_missing_or_empty_file(tmp_path):
    assert srt.page_impressions(str(tmp_path / "none.jsonl")) == {}
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert srt.page_impressions(str(p)) == {}
    p.write_text("broken\n", encoding="utf-8")
    assert srt.page_impressions(str(p)) == {}


def test_page_impressions_ignores_invalid_iso_dates(tmp_path):
    p = tmp_path / "gsc.jsonl"
    _jsonl(p, _gsc_rows(RW, ["2026-11-11"], 200) + [
        {"date": "zzzzzzzzzz", "page": f"https://navi.omcha.jp/products/{NEW.lower()}/", "impressions": 999}
    ])
    assert srt.page_impressions(str(p)) == {RW: 200}


def test_select_main_excludes_high_impression_page(tmp_path, monkeypatch, capsys):
    arts = tmp_path / "articles"
    for asin in (RW, RW_STOCK):
        _write(arts / f"2026-05-01-{asin}.json", {"date": "2026-05-01"})
    gsc = tmp_path / "gsc.jsonl"
    _jsonl(gsc, _gsc_rows(RW, ["2026-11-11"], 250))
    out = tmp_path / "out.txt"
    monkeypatch.setattr(srt, "sources_exhausted", lambda a: False)
    monkeypatch.setattr(sys, "argv", [
        "select_rewrite_targets.py", "--articles-dir", str(arts), "--limit", "5",
        "--out", str(out), "--queue-dir", str(tmp_path / "queue"), "--gsc-by-page", str(gsc)])
    assert srt.main() == 0
    assert out.read_text(encoding="utf-8").split() == [RW_STOCK]
    assert f"protected" in capsys.readouterr().err


# ---------------------------------------------------------------- Jules への注記


def _ctx(monkeypatch):
    import build_asin_query_context as bq
    monkeypatch.setattr(bq, "build_context", lambda asin: {
        "fallback": False, "queries": [{"query": "q", "impressions": 10, "position": 5.0}]})


def test_gsc_note_for_rewrite_does_not_ask_to_change_title(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ctx(monkeypatch)
    _write(tmp_path / "data" / "articles" / f"2026-05-01-{RW}.json", {})
    note = build_jules_prompt._gsc_note(RW)
    assert build_jules_prompt.GSC_TITLE_LINE_REWRITE in note
    assert "最優先のシグナル" not in note


def test_gsc_note_for_new_article_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ctx(monkeypatch)
    note = build_jules_prompt._gsc_note(NEW)
    assert note == (
        "【このページに実際に流入している検索クエリ (GSC 直近4週)】\n"
        "- 「q」 (表示 10 回 / 平均掲載順位 5.0)\n"
        "- これは読者が実際に検索窓に打った語です。title / meta_description の検索意図選定 "
        "(テンプレート §4 の意図差別化) では、このクエリ群を最優先のシグナルとして使ってください\n"
        "- faq のうち最低 1 問は、上記クエリが示す疑問への直接回答にしてください (クエリの語をそのまま質問文に活かす)\n"
        "- クエリが示す疑問 (例: 「○○ 対象年齢」「○○ 違い」) に本文が答えていない場合、"
        "narrative の該当セクションで必ず回答してください"
    )


def test_workflow_03_uses_same_rewrite_line():
    yml = (ROOT.parent / ".github" / "workflows" / "03-invoke-jules.yml").read_text(encoding="utf-8")
    assert build_jules_prompt.GSC_TITLE_LINE_REWRITE in yml
    assert build_jules_prompt.GSC_TITLE_LINE_NEW in yml
