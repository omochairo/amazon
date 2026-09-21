"""#7957 (#7955 A): 手順の無い「遊び方」の約束を止める。

Coverage:
1. official_howto: 約束語の検出 / official_howto.json の読み込み / url・reviewed steps の判定
2. filter_howto_faq: reviewed steps が無ければ手順を問う FAQ を落とし、事実の FAQ は残す
3. quality_gate.check_howto_title_promise:
   約束あり×official_howto 無し → 施行日以降 fail・施行日前 soft /
   約束あり×url あり → pass / 約束なし → pass
4. TITLE_SERP_INTENT_WORDS から「遊び方」が外れている
5. テンプレートの見出しが「家庭での遊ばれ方」に変わっている
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import official_howto as oh  # noqa: E402
from quality_gate import (  # noqa: E402
    HOWTO_TITLE_PROMISE_ENFORCE_FROM,
    TITLE_SERP_INTENT_WORDS,
    check_howto_title_promise,
)

ASIN = "B0H4PQ29JS"
URL = "https://toy.bandai.co.jp/manuals/pdf.php?id=2852540"


def _write(root: pathlib.Path, obj: dict) -> None:
    d = root / ASIN
    d.mkdir(parents=True, exist_ok=True)
    (d / "official_howto.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _article(title: str, slug_date: str = "2026-09-25") -> dict:
    return {"title": title, "slug": f"{slug_date}-{ASIN}", "product": {"asin": ASIN, "name": "デジヴァイス"}}


# --- 1. official_howto ---------------------------------------------------------

def test_find_howto_promise_detects_words():
    assert oh.find_howto_promise("デジヴァイスの遊び方・口コミ") == "遊び方"
    assert oh.find_howto_promise("レゴ 60312 説明書") == "説明書"
    assert oh.find_howto_promise("シャインアルカナロッドの口コミ・最安値") is None
    assert oh.find_howto_promise(None) is None


def test_load_missing_and_broken(tmp_path):
    assert oh.load(ASIN, tmp_path) is None
    (tmp_path / ASIN).mkdir()
    (tmp_path / ASIN / "official_howto.json").write_text("{broken", encoding="utf-8")
    assert oh.load(ASIN, tmp_path) is None
    assert oh.load(None, tmp_path) is None


def test_load_is_case_insensitive_on_asin(tmp_path):
    _write(tmp_path, {"url": URL})
    assert oh.load(ASIN.lower(), tmp_path) == {"url": URL}


def test_has_official_url_and_reviewed_steps():
    assert not oh.has_official_url(None)
    assert not oh.has_official_url({"status": "not_found"})
    assert not oh.has_official_url({"url": "http://insecure.example/"})
    assert oh.has_official_url({"url": URL})
    # steps があっても人が読んでいなければ出さない
    assert not oh.has_reviewed_steps({"url": URL, "steps": [{"text": "a"}]})
    assert not oh.has_reviewed_steps({"url": URL, "steps": [], "reviewed_by": "iromama"})
    assert not oh.has_reviewed_steps({"steps": [{"text": "a"}], "reviewed_by": "iromama"})
    assert oh.has_reviewed_steps({"url": URL, "steps": [{"text": "a"}], "reviewed_by": "iromama"})


# --- 2. filter_howto_faq -------------------------------------------------------

FAQ = [
    {"question": "デジヴァイスは何歳から遊べますか？", "answer": "8歳以上です。"},
    {"question": "デジヴァイスの電池は何を使用しますか？", "answer": "CR2032 を1個です。"},
    {"question": "デジヴァイスの遊び方は？", "answer": "ボタン操作による育成を楽しむゲームです。"},
    {"question": "使い方は難しいですか？", "answer": "簡単です。"},
]


def test_filter_drops_howto_questions_without_steps():
    kept, dropped = oh.filter_howto_faq(FAQ, None)
    assert dropped == 2
    assert [q["question"] for q in kept] == [FAQ[0]["question"], FAQ[1]["question"]]


def test_filter_drops_even_with_url_only():
    kept, dropped = oh.filter_howto_faq(FAQ, {"url": URL})
    assert dropped == 2 and len(kept) == 2


def test_filter_keeps_all_with_reviewed_steps():
    obj = {"url": URL, "steps": [{"text": "リセットスイッチを押す"}], "reviewed_by": "iromama"}
    kept, dropped = oh.filter_howto_faq(FAQ, obj)
    assert dropped == 0 and kept == FAQ


def test_filter_passes_non_list_through():
    assert oh.filter_howto_faq(None, None) == (None, 0)


# --- 3. quality_gate -----------------------------------------------------------

def test_gate_no_promise_passes(tmp_path):
    r = check_howto_title_promise(_article("デジヴァイスの口コミ・最安値"), tmp_path)
    assert r.passed and r.score == 1.0


def test_gate_promise_without_source_fails_after_enforce(tmp_path):
    r = check_howto_title_promise(_article("デジヴァイスの遊び方・口コミ", HOWTO_TITLE_PROMISE_ENFORCE_FROM), tmp_path)
    assert not r.passed
    assert "howto-promise-without-official-source" in r.message


def test_gate_promise_without_source_is_soft_before_enforce(tmp_path):
    r = check_howto_title_promise(_article("デジヴァイスの遊び方・口コミ", "2026-09-01"), tmp_path)
    assert r.passed and r.score < 1.0


def test_gate_promise_with_official_url_passes(tmp_path):
    _write(tmp_path, {"url": URL, "publisher": "bandai"})
    r = check_howto_title_promise(_article("デジヴァイスの遊び方・口コミ"), tmp_path)
    assert r.passed and r.score == 1.0


def test_gate_not_found_record_is_not_a_source(tmp_path):
    _write(tmp_path, {"asin": ASIN, "status": "not_found"})
    r = check_howto_title_promise(_article("デジヴァイスの遊び方・口コミ"), tmp_path)
    assert not r.passed


# --- 4 / 5 ---------------------------------------------------------------------

def test_intent_words_no_longer_recommend_asobikata():
    assert "遊び方" not in TITLE_SERP_INTENT_WORDS


def test_template_heading_renamed():
    tpl = (ROOT / "templates" / "post.md.j2").read_text(encoding="utf-8")
    assert "実際の使い方・遊び方" not in tpl
    assert "🎮 家庭での遊ばれ方" in tpl
