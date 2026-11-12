"""navi-brain#164 R2: 商品ページの「購入者の声」(hugo/data/voices/<ASIN>.json) の決まりを検査する。

書式::

    {
      "asin": "B0XXXXXXXX",            # ファイル名と同じ
      "collected_on": "YYYY-MM-DD",    # 声を集めた日
      "show_until": "YYYY-MM-DD",      # この日を過ぎたらページに出さない（集めた日から 1 年以内）
      "origin": "MUSE-003",            # どの調査から来たか（成果の記録 R3 で数える）
      "subject": "",                   # 声が商品そのものでなくシリーズ全体のときに書く（空でよい）
      "scope": "Instagram・Threads・X の投稿と、…",   # どこから集めたか（出典の種類）
      "excluded_promotions": 2,        # 数えなかった宣伝の投稿の数
      "good": [{"text": "...", "count": 4, "where": "Instagram の実演動画"}],
      "concerns": [...],               # 不満も必ず載せる（1 件以上）
      "age": [...]                     # 年齢の実感（任意）
    }
"""
import datetime as dt
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
VOICES = REPO / "hugo" / "data" / "voices"
FILES = sorted(VOICES.glob("*.json"))
# 読む人が引っかかる言い方（navi-brain#164 R2）と、個別の値引きの断定（#6285）
BANNED = ("未確認", "要確認", "未検証", "真偽", "噂", "値引き", "割引", "% OFF", "%OFF")
GROUPS = ("good", "concerns", "age")


def _live_asins() -> set[str]:
    out = set()
    for f in (REPO / "data" / "articles").glob("*.json"):
        m = re.search(r"-(B0[A-Z0-9]{8})$", f.stem)
        if m:
            out.add(m.group(1))
    return out


def test_there_is_at_least_one_voices_file():
    assert FILES, "hugo/data/voices/ が空（R2 の最初の 1 商品が消えている）"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_voices_file_follows_the_rules(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["asin"] == path.stem
    assert d["asin"] in _live_asins(), "記事の無い商品の声（ページに出ない）"
    collected = dt.date.fromisoformat(d["collected_on"])
    until = dt.date.fromisoformat(d["show_until"])
    assert collected < until <= collected + dt.timedelta(days=366), "show_until は集めた日から 1 年以内"
    assert re.fullmatch(r"MUSE-\d{3,}", d["origin"]) or d["origin"] == "manual"
    assert d["scope"].strip()
    assert isinstance(d["excluded_promotions"], int) and d["excluded_promotions"] >= 0
    assert d.get("good") and d.get("concerns"), "良い声と不満の両方を載せる"
    for g in GROUPS:
        for row in d.get(g, []):
            assert set(row) == {"text", "count", "where"}
            assert isinstance(row["count"], int) and row["count"] >= 1
            assert row["where"].strip()
            text = row["text"]
            assert 8 <= len(text) <= 80, f"要点は短く自分の言葉で: {text}"
            assert "http" not in text and "@" not in text, "URL・アカウント名を載せない"
            assert not re.search(r"[「『].{16,}[」』]", text), "16 字を超える引用（丸写し）をしない"
    blob = json.dumps(d, ensure_ascii=False)
    for w in BANNED:
        assert w not in blob, f"使わない言い方: {w}"
    assert not re.search(r"[0-9０-９,]+\s*円", blob), "価格を書かない（値は記事本文の工程で公式と照らす）"


def test_partial_hides_expired_voices():
    """show_until を過ぎた声を出さない判定が partial にあること（取り下げの決まり）。"""
    src = (REPO / "hugo" / "layouts" / "partials" / "buyer_voices.html").read_text(encoding="utf-8")
    assert "show_until" in src and 'now.Format "2006-01-02"' in src
    single = (REPO / "hugo" / "layouts" / "posts" / "single.html").read_text(encoding="utf-8")
    assert 'partial "buyer_voices.html"' in single
