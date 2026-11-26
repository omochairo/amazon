"""scripts/ci_indexnow.py の選び方と、送る前の確かめ。ネットワークには出ない。"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_indexnow", ROOT / "scripts" / "ci_indexnow.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)

KEY = "0123456789abcdef0123456789abcdef"
NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
B = "https://navi.omcha.jp"


def _diff(paths: list[str], declared: int | None = None) -> str:
    n = len(paths) if declared is None else declared
    body = "".join(f"navi-diff\t{p}\n" for p in paths)
    return f"navi-diff-begin\n{body}navi-diff-end\t{n}\nswitched to abc\n"


def _sitemap(entries: dict[str, str | None]) -> str:
    rows = []
    for loc, lm in entries.items():
        lmx = f"<lastmod>{lm}</lastmod>" if lm else ""
        rows.append(f"<url><loc>{loc}</loc>{lmx}</url>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(rows) + "</urlset>"
    )


@pytest.fixture
def site(tmp_path):
    pub = tmp_path / "public"
    pub.mkdir()
    (pub / f"{KEY}.txt").write_text(KEY, encoding="utf-8")
    (pub / "sitemap.xml").write_text(
        _sitemap(
            {
                f"{B}/": "2026-11-26T10:00:00+09:00",
                f"{B}/products/new1/": "2026-11-25T09:00:00+09:00",  # 新しい記事
                f"{B}/products/old1/": "2026-10-01T09:00:00+09:00",  # 価格だけ変わった
                f"{B}/products/nolm/": None,
            }
        ),
        encoding="utf-8",
    )
    return tmp_path, pub


def _write_diff(tmp_path, content):
    p = tmp_path / "navi-switch.out"
    p.write_text(content, encoding="utf-8")
    return p


def test_picks_changed_and_recent_and_in_sitemap(site):
    tmp, pub = site
    diff = _write_diff(
        tmp,
        _diff(
            [
                "index.html",
                "products/new1/index.html",
                "products/old1/index.html",  # lastmod が古い → 送らない
                "products/nolm/index.html",  # lastmod 無し → 送らない
                "products/gone/index.html",  # sitemap に無い (noindex・削除) → 送らない
                "js/app.123.js",  # ページではない
                "build.json",
            ]
        ),
    )
    urls = ci.select_urls(ci.read_changed_paths(diff), ci.read_sitemap(pub / "sitemap.xml"), NOW, 72)
    assert urls == [f"{B}/", f"{B}/products/new1/"]


@pytest.mark.parametrize(
    "content",
    [
        "navi-diff-unknown\n",
        "garbage\n",
        _diff(["products/new1/index.html"], declared=5),
    ],
)
def test_unreadable_diff_falls_back_to_recent_lastmod(site, content):
    tmp, pub = site
    diff = _write_diff(tmp, content)
    assert ci.read_changed_paths(diff) is None
    urls = ci.select_urls(None, ci.read_sitemap(pub / "sitemap.xml"), NOW, 72)
    assert urls == [f"{B}/", f"{B}/products/new1/"]


def test_missing_diff_file_falls_back(site):
    tmp, _ = site
    assert ci.read_changed_paths(tmp / "nope.out") is None


def test_no_change_sends_nothing(site):
    tmp, pub = site
    diff = _write_diff(tmp, _diff(["build.json"]))
    calls = []
    rc = ci.run(diff, pub, NOW, 72, False, get=lambda u: calls.append(u), post=lambda u, b: calls.append(u))
    assert rc == 0 and calls == []


def test_sends_after_live_key_check(site):
    tmp, pub = site
    diff = _write_diff(tmp, _diff(["products/new1/index.html"]))
    sent = {}

    def post(url, body):
        sent["url"], sent["body"] = url, body
        return 202, ""

    rc = ci.run(diff, pub, NOW, 72, False, get=lambda u: (200, KEY + "\n"), post=post)
    assert rc == 0
    assert sent["url"] == ci.ENDPOINT
    assert sent["body"] == {
        "host": "navi.omcha.jp",
        "key": KEY,
        "keyLocation": f"{B}/{KEY}.txt",
        "urlList": [f"{B}/products/new1/"],
    }


@pytest.mark.parametrize("status,text", [(404, ""), (200, "different"), (200, "")])
def test_live_key_mismatch_fails_without_sending(site, status, text):
    tmp, pub = site
    diff = _write_diff(tmp, _diff(["products/new1/index.html"]))
    posted = []
    rc = ci.run(diff, pub, NOW, 72, False, get=lambda u: (status, text), post=lambda u, b: posted.append(b))
    assert rc == 1 and posted == []


@pytest.mark.parametrize("status", [400, 403, 422, 429, 500])
def test_rejected_submission_fails(site, status):
    tmp, pub = site
    diff = _write_diff(tmp, _diff(["products/new1/index.html"]))
    rc = ci.run(diff, pub, NOW, 72, False, get=lambda u: (200, KEY), post=lambda u, b: (status, "x"))
    assert rc == 1


def test_over_limit_is_split_not_dropped(site, monkeypatch):
    tmp, pub = site
    monkeypatch.setattr(ci, "MAX_URLS", 1)
    diff = _write_diff(tmp, _diff(["index.html", "products/new1/index.html"]))
    bodies = []
    rc = ci.run(diff, pub, NOW, 72, False, get=lambda u: (200, KEY),
                post=lambda u, b: (bodies.append(b["urlList"]), (200, ""))[1])
    assert rc == 0
    assert bodies == [[f"{B}/"], [f"{B}/products/new1/"]]


def test_dry_run_does_not_touch_network(site):
    tmp, pub = site
    diff = _write_diff(tmp, _diff(["products/new1/index.html"]))
    calls = []
    rc = ci.run(diff, pub, NOW, 72, True, get=lambda u: calls.append(u), post=lambda u, b: calls.append(u))
    assert rc == 0 and calls == []


def test_key_must_be_unique(tmp_path):
    pub = tmp_path / "public"
    pub.mkdir()
    with pytest.raises(SystemExit):
        ci.find_key(pub)
    (pub / f"{KEY}.txt").write_text(KEY, encoding="utf-8")
    (pub / "robots.txt").write_text("x", encoding="utf-8")
    # 中身がファイル名と違うものは鍵とみなさない
    (pub / "fedcba9876543210fedcba9876543210.txt").write_text("nope", encoding="utf-8")
    assert ci.find_key(pub) == KEY


def test_repo_has_exactly_one_key_file():
    # 本番の鍵 (hugo/static/<鍵>.txt) が消えたり 2 つになったりしたら気づけるように
    assert KEY_RE_MATCHES(ROOT / "hugo" / "static") == 1


def KEY_RE_MATCHES(folder: pathlib.Path) -> int:
    n = 0
    for p in folder.glob("*.txt"):
        if ci.KEY_RE.match(p.stem):
            assert p.read_text(encoding="utf-8").strip() == p.stem
            n += 1
    return n
