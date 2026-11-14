from card_reason import card_reason


def test_headline_used():
    assert card_reason({"verdict": {"headline": "  \u3088\u304f\u8003\u3048\u3066\u904a\u3079\u308b  "}}, 80) == "\u3088\u304f\u8003\u3048\u3066\u904a\u3079\u308b"


def test_low_score_skips_headline():
    article = {"verdict": {"headline": "\u8cb7\u3044\u3067\u3059"}, "persona_fit": {"recommended_for": ["\u89aa\u5b50"]}}
    assert card_reason(article, 60) == "\u5411\u304f\u4eba\uff1a\u89aa\u5b50"


def test_fallback_to_recommended_for():
    assert card_reason({"persona_fit": {"recommended_for": ["\u96c6\u4e2d\u3057\u3066\u904a\u3073\u305f\u3044\u5b50"]}}) == "\u5411\u304f\u4eba\uff1a\u96c6\u4e2d\u3057\u3066\u904a\u3073\u305f\u3044\u5b50"


def test_fallback_to_pros():
    assert card_reason({"product": {"pros": ["\u6728\u88fd\u30d1\u30fc\u30c4\u3067\u9577\u304f\u904a\u3079\u308b"]}}) == "\u6728\u88fd\u30d1\u30fc\u30c4\u3067\u9577\u304f\u904a\u3079\u308b"


def test_truncation_and_prefix():
    result = card_reason({"verdict": {"headline": "\u304a\u3082\u3061\u3083\u30ed\u30dc\u3088\u308a\uff1a" + "\u3042" * 61}})
    assert result == "\u3042" * 60 + "\u2026"


def test_malformed_input_returns_none():
    assert card_reason([]) is None
    assert card_reason({"verdict": {"headline": ["no"]}, "persona_fit": None, "product": {"pros": [None]}}) is None


def test_blank_recommended_for_falls_back_to_pros():
    article = {"persona_fit": {"recommended_for": ["   "]}, "product": {"pros": ["長く遊べる"]}}
    assert card_reason(article) == "長く遊べる"
