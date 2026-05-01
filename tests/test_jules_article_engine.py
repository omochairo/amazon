import sys
import os
from unittest.mock import MagicMock

# Add scripts directory to sys.path to allow importing modules from it
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

# Mock dependencies that might be missing or trigger network calls
sys.modules['requests'] = MagicMock()
sys.modules['read_raw'] = MagicMock()
sys.modules['history_check'] = MagicMock()
sys.modules['internal_links'] = MagicMock()
sys.modules['google-api-python-client'] = MagicMock()
sys.modules['googleapiclient'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()
sys.modules['pytrends'] = MagicMock()
sys.modules['pytrends.request'] = MagicMock()
sys.modules['rakuten_ws'] = MagicMock()

from jules_article_engine import generate_pros_cons

def test_generate_pros_cons_with_multiple_features():
    item = {
        "features": ["Feature 1", "Feature 2", "Feature 3"],
        "price": 5000
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["Feature 1", "Feature 2"]
    assert cons == ["特になし"]

def test_generate_pros_cons_with_single_feature():
    item = {
        "features": ["Only One Feature"],
        "price": 5000
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["Only One Feature"]
    assert cons == ["特になし"]

def test_generate_pros_cons_with_no_features():
    item = {
        "features": [],
        "price": 5000
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["評価が高い", "定番商品"]
    assert cons == ["特になし"]

def test_generate_pros_cons_missing_features_key():
    item = {
        "price": 5000
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["評価が高い", "定番商品"]
    assert cons == ["特になし"]

def test_generate_pros_cons_high_price():
    item = {
        "features": ["F1", "F2"],
        "price": 10001
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["F1", "F2"]
    assert cons == ["少し高価かも"]

def test_generate_pros_cons_threshold_price():
    item = {
        "features": ["F1", "F2"],
        "price": 10000
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["F1", "F2"]
    assert cons == ["特になし"]

def test_generate_pros_cons_missing_price():
    item = {
        "features": ["F1", "F2"]
    }
    pros, cons = generate_pros_cons(item)
    assert pros == ["F1", "F2"]
    assert cons == ["特になし"]
