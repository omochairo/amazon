"""#7959 (#7955 C 改訂版): 「📘 公式の取扱説明書・遊び方」ブロックの
決定的レンダリング層 (official_howto_format.py) のユニットテスト。

Coverage:
1. url が無い/レコードが無い -> None (テンプレは何も描画しない)
2. url のみ (steps 無し) -> リンクのみのブロック。日付・official_name あり
3. reviewed steps あり -> 番号つき手順 + 出典の節 + 全文リンク
4. reviewed steps 無し (reviewed_by 欠落) -> リンクのみに落ちる
5. HTML エスケープ (official_name / step text / section)
6. リンクの rel は noopener のみ (sponsored/nofollow を含まない)
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_howto_format import build_official_howto_block  # noqa: E402

URL = "https://toy.bandai.co.jp/manuals/pdf.php?id=2852540"


# --- 1. no url / no record ------------------------------------------------

def test_none_object_returns_none():
    assert build_official_howto_block(None) is None


def test_not_found_status_returns_none():
    assert build_official_howto_block({"asin": "B0X", "status": "not_found"}) is None


def test_missing_url_returns_none():
    assert build_official_howto_block({"publisher": "bandai", "kind": "manual_pdf"}) is None


def test_insecure_url_returns_none():
    assert build_official_howto_block({"url": "http://insecure.example/"}) is None


# --- 2. url only (no steps) ------------------------------------------------

def test_url_only_block_has_link_and_date():
    obj = {
        "publisher": "bandai",
        "kind": "manual_pdf",
        "url": URL,
        "official_name": "DIGIVICE Ver.REVIVAL 石田ヤマトカラー",
        "fetched_at": "2026-09-21T15:45:04Z",
    }
    block = build_official_howto_block(obj)
    assert block is not None
    assert block["has_steps"] is False
    assert block["steps"] == []
    assert "バンダイが公開している取扱説明書（PDF）" in block["intro"]
    assert "DIGIVICE Ver.REVIVAL 石田ヤマトカラー" in block["intro"]
    assert block["url"] == URL
    assert block["link_text"] == "公式サイトで見る →"
    assert block["date_note"] == "（2026-09-21 時点で公開を確認）"
    assert block["date_label"] == "2026-09-21"


def test_url_only_without_official_name_or_date():
    obj = {"publisher": "lego", "kind": "building_instructions", "url": URL}
    block = build_official_howto_block(obj)
    assert block is not None
    assert block["intro"] == "レゴが公開している組み立て説明書です。"
    assert block["date_note"] is None
    assert block["date_label"] is None


def test_unknown_publisher_and_kind_fall_back_to_generic_labels():
    obj = {"publisher": "acme", "kind": "mystery", "url": URL}
    block = build_official_howto_block(obj)
    assert block is not None
    assert block["intro"] == "メーカーが公開している取扱説明書です。"


# --- 3. reviewed steps ------------------------------------------------------

def _reviewed_obj(steps=None):
    return {
        "publisher": "bandai",
        "kind": "manual_pdf",
        "url": URL,
        "official_name": "デジヴァイス",
        "fetched_at": "2026-09-21T15:45:04Z",
        "reviewed_by": "iromama",
        "steps": steps if steps is not None else [
            {"text": "裏のリセットスイッチを押してから遊び始める", "section": "【1】遊ぶ前の準備"},
            {"text": "最初のパートナーを7体から選ぶ", "section": "【5】パートナーデジモンを選ぼう！"},
        ],
    }


def test_reviewed_steps_render_ordered_list_with_sources():
    block = build_official_howto_block(_reviewed_obj())
    assert block["has_steps"] is True
    assert block["intro"] == "バンダイ公式の取扱説明書（PDF）から、はじめ方をまとめました。"
    assert len(block["steps"]) == 2
    assert block["steps"][0] == {
        "text": "裏のリセットスイッチを押してから遊び始める",
        "section": "【1】遊ぶ前の準備",
    }
    assert block["closing_link_text"] == "▶ 取扱説明書（PDF）の全文はバンダイ公式サイトで"
    assert block["date_label"] == "2026-09-21"


def test_reviewed_steps_without_section_omits_it():
    block = build_official_howto_block(_reviewed_obj(steps=[{"text": "ボタンを押す"}]))
    assert block["steps"] == [{"text": "ボタンを押す", "section": None}]


def test_reviewed_steps_drops_entries_without_text():
    block = build_official_howto_block(_reviewed_obj(steps=[
        {"section": "no text here"},
        {"text": "  ", "section": "blank text"},
        {"text": "有効な手順", "section": "【2】"},
    ]))
    assert block["steps"] == [{"text": "有効な手順", "section": "【2】"}]


def test_reviewed_but_all_steps_invalid_falls_back_to_link_only():
    block = build_official_howto_block(_reviewed_obj(steps=[{"section": "no text"}]))
    assert block["has_steps"] is False
    assert block["steps"] == []
    assert "link_text" in block


# --- 4. steps present but not reviewed (no reviewed_by) --------------------

def test_steps_without_reviewed_by_falls_back_to_link_only():
    obj = {
        "publisher": "bandai",
        "kind": "manual_pdf",
        "url": URL,
        "steps": [{"text": "ボタンを押す", "section": "【1】"}],
        # reviewed_by 無し
    }
    block = build_official_howto_block(obj)
    assert block["has_steps"] is False
    assert block["steps"] == []


def test_empty_steps_list_with_reviewed_by_falls_back_to_link_only():
    obj = {
        "publisher": "bandai", "kind": "manual_pdf", "url": URL,
        "steps": [], "reviewed_by": "iromama",
    }
    block = build_official_howto_block(obj)
    assert block["has_steps"] is False


# --- 5. HTML escaping --------------------------------------------------------

def test_official_name_is_html_escaped():
    obj = {"publisher": "bandai", "kind": "manual_pdf", "url": URL,
           "official_name": '<script>alert("x")</script>'}
    block = build_official_howto_block(obj)
    assert "<script>" not in block["intro"]
    assert "&lt;script&gt;" in block["intro"]


def test_step_text_and_section_are_html_escaped():
    obj = _reviewed_obj(steps=[
        {"text": "<b>強調</b> & 続き", "section": "<i>節</i>"},
    ])
    block = build_official_howto_block(obj)
    step = block["steps"][0]
    assert "<b>" not in step["text"]
    assert "&lt;b&gt;" in step["text"]
    assert "&amp;" in step["text"]
    assert "<i>" not in step["section"]
    assert "&lt;i&gt;" in step["section"]


def test_url_is_escaped_for_attribute_safety():
    obj = {"publisher": "bandai", "kind": "manual_pdf",
           "url": 'https://example.com/x?a=1&b="2"'}
    block = build_official_howto_block(obj)
    assert "&quot;" in block["url"]
    assert "&amp;" in block["url"]


# --- 6. rel attribute discipline (checked against the rendered template) ---

def test_no_sponsored_or_nofollow_words_anywhere_in_block_values():
    block = build_official_howto_block(_reviewed_obj())
    flat = " ".join(str(v) for v in block.values() if isinstance(v, str))
    flat += " ".join(
        f"{s.get('text','')} {s.get('section') or ''}" for s in block["steps"]
    )
    assert "sponsored" not in flat
    assert "nofollow" not in flat


def test_template_renders_link_only_variant_with_noopener_and_no_sponsored():
    """post.md.j2 のリンク行が noopener のみで sponsored/nofollow を含まないこと
    を、実際に Jinja でレンダリングして確認する (テンプレの記述ミス検知)。"""
    import jinja2

    tpl_dir = ROOT / "templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(tpl_dir)), autoescape=False)
    template_src = (tpl_dir / "post.md.j2").read_text(encoding="utf-8")
    obj = {"publisher": "lego", "kind": "building_instructions", "url": URL}
    block = build_official_howto_block(obj)
    # official-howto-block だけを取り出すため、テンプレ全体をレンダリングせず
    # 該当 if ブロックのみを抽出してレンダリングする。
    start = template_src.index("{% if official_howto_block %}")
    end = template_src.index("{% if narrative.daily_use %}")
    snippet = template_src[start:end]
    rendered = env.from_string(snippet).render(
        official_howto_block=block, product={"name": "テスト商品"},
    )
    assert 'rel="noopener"' in rendered
    assert "sponsored" not in rendered
    assert "nofollow" not in rendered
    assert 'target="_blank"' in rendered
    assert "公式サイトで見る →" in rendered
