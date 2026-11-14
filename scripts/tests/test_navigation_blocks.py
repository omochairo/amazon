import json
import pathlib
import tempfile
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import build_post as bp


def entry(asin, score=80, age=24, types=("puzzle",)):
    return {"slug": asin, "ivs_score_100": score, "name": f"商品 {asin}",
            "image": "", "amazon_price": 3000, "age_min_months": age,
            "toy_types": list(types)}


def article(asin="SELF", age="2歳〜", tags=None):
    return {"product": {"asin": asin, "name": "計算パズル", "target_age": age},
            "tags": tags or ["計算", "パズル"], "keywords": ["算数"],
            "competitive_analysis": []}


def test_next_steps_resolves_existing_hubs_and_price_band():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "hub_index.json").write_text(json.dumps([
            {"key": "age-2", "label": "2歳", "url": "/toys-age-2/"},
            {"key": "math", "label": "算数", "url": "/math-toys/"}]), encoding="utf-8")
        for key in ("age-2", "math"):
            (root / f"{key}.json").write_text("{}", encoding="utf-8")
        data = article()
        data["same_price_band"] = {"band_url": "/cospa/#one"}
        bp._recommend_next_steps(data, {"SELF": entry("SELF")}, "", feature_root=root)
        urls = [x["url"] for x in data["next_steps"]]
        assert "/toys-age-2/" in urls and "/math-toys/" in urls and "/cospa/#one" in urls


def test_alternatives_low_score_and_excludes_self_and_shown_articles():
    data = article()
    data["competitive_analysis"] = [{"asin": "SHOWN"}]
    data["same_price_band"] = {"cards": [{"asin": "BAND"}]}
    idx = {"SELF": entry("SELF"), "SHOWN": entry("SHOWN", 99), "BAND": entry("BAND", 98)}
    idx.update({f"C{i}": entry(f"C{i}", 90 - i) for i in range(1, 5)})
    bp._recommend_alternatives(data, idx, "", 60)
    assert len(data["alternatives"]) == 3
    assert not ({"SELF", "SHOWN", "BAND"} & {c["asin"] for c in data["alternatives"]})
    bp._recommend_alternatives(data, idx, "", 61)
    assert data["alternatives"] == []


def test_alternatives_requires_three_candidates():
    data = article()
    bp._recommend_alternatives(data, {"SELF": entry("SELF"), "A": entry("A"), "B": entry("B")}, "", 60)
    assert data["alternatives"] == []
