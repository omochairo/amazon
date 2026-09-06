"""埋め込みキャッシュ (#6602 N3a) の unit tests。

このキャッシュが壊れる壊れ方は 2 通りあって、片方は静かに間違える:

1. **落ちる** — 壊れた JSON、書けないディレクトリ。これはレーンを止めては
   いけない (速いだけの仕組みで本番を止めない)
2. **黙って別モデルのベクタを配る** — ruri の EMBED_MODEL を差し替えたのに
   キーが変わらない場合。コサイン類似度が意味を失うが、エラーは出ない

2 の方が危ないので、モデル ID がキーに効いていることを重点的に固定する。
"""
from __future__ import annotations

import json

import pytest

from scripts import compute_semantic_related as csr


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise csr.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    """/health と /embed を返すだけの最小セッション。"""

    def __init__(self, embed_model="cl-nagoya/ruri-v3-310m", vector=(0.1, 0.2)):
        self.embed_model = embed_model
        self.vector = list(vector)
        self.embed_calls: list[list[str]] = []

    def get(self, url, timeout=None):
        return _FakeResp({"status": "ok", "embed_model": self.embed_model})

    def post(self, url, json=None, timeout=None):
        texts = (json or {}).get("texts") or (json or {}).get("input") or []
        self.embed_calls.append(list(texts))
        return _FakeResp({"vectors": [self.vector for _ in texts],
                          "embeddings": [self.vector for _ in texts]})


# --------------------------------------------------------------------------
# キー — モデル ID が効いていること
# --------------------------------------------------------------------------

def test_cache_key_changes_with_model():
    a = csr.cache_key("model-a", "同じテキスト")
    b = csr.cache_key("model-b", "同じテキスト")
    assert a != b


def test_cache_key_changes_with_text():
    a = csr.cache_key("m", "テキスト1")
    b = csr.cache_key("m", "テキスト2")
    assert a != b


def test_resolve_embed_model_uses_server_name_for_ruri():
    """ruri の --model はラベルでしかない。実モデルは /health が名乗る方。"""
    s = _FakeSession(embed_model="cl-nagoya/ruri-v3-70m")
    got = csr.resolve_embed_model("ruri", model="ラベルにすぎない",
                                  ruri_url="http://ruri:8000", session=s)
    assert got == "cl-nagoya/ruri-v3-70m"


def test_resolve_embed_model_uses_caller_model_for_ollama():
    s = _FakeSession()
    got = csr.resolve_embed_model("ollama", model="nomic-embed-text",
                                  ruri_url="http://ruri:8000", session=s)
    assert got == "nomic-embed-text"


def test_resolve_embed_model_returns_empty_when_health_fails():
    """実モデルが分からないならキャッシュしない。遅くても正しい方を採る。"""
    class Boom(_FakeSession):
        def get(self, url, timeout=None):
            raise csr.requests.ConnectionError("no route")

    got = csr.resolve_embed_model("ruri", model="x", ruri_url="http://ruri:8000",
                                  session=Boom())
    assert got == ""


# --------------------------------------------------------------------------
# ロード/セーブ
# --------------------------------------------------------------------------

def test_cache_roundtrip(tmp_path):
    p = tmp_path / "cache.json"
    c = csr.EmbeddingCache(p, "m1")
    c.load()
    assert c.get("あ") is None
    c.put("あ", [1.0, 2.0])
    c.save()

    c2 = csr.EmbeddingCache(p, "m1")
    c2.load()
    assert c2.get("あ") == [1.0, 2.0]


def test_cache_discarded_when_model_changes(tmp_path):
    p = tmp_path / "cache.json"
    c = csr.EmbeddingCache(p, "m1")
    c.put("あ", [1.0])
    c.save()

    c2 = csr.EmbeddingCache(p, "m2")   # モデルが変わった
    c2.load()
    assert c2.get("あ") is None


def test_corrupt_cache_does_not_raise(tmp_path):
    """壊れたキャッシュでレーンを止めない。"""
    p = tmp_path / "cache.json"
    p.write_text("{ this is not json", encoding="utf-8")
    c = csr.EmbeddingCache(p, "m1")
    c.load()
    assert c.vectors == {}
    assert c.get("あ") is None


def test_cache_with_wrong_format_is_discarded(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"format": 999, "model": "m1",
                             "vectors": {"k": [1.0]}}), encoding="utf-8")
    c = csr.EmbeddingCache(p, "m1")
    c.load()
    assert c.vectors == {}


def test_save_prunes_entries_not_seen_this_run(tmp_path):
    """削除された記事のベクタが永久に溜まらないこと。"""
    p = tmp_path / "cache.json"
    c = csr.EmbeddingCache(p, "m1")
    c.put("残す", [1.0])
    c.put("消える", [2.0])
    c.save()

    c2 = csr.EmbeddingCache(p, "m1")
    c2.load()
    assert len(c2.vectors) == 2
    c2.get("残す")          # 今回参照したのはこれだけ
    c2.save()

    c3 = csr.EmbeddingCache(p, "m1")
    c3.load()
    assert c3.get("残す") == [1.0]
    assert c3.get("消える") is None


def test_disabled_cache_is_inert(tmp_path):
    """path が無い / モデル ID が空なら何もしない。"""
    for cache in (csr.EmbeddingCache(None, "m1"),
                  csr.EmbeddingCache(tmp_path / "c.json", "")):
        assert not cache.enabled
        cache.load()
        cache.put("あ", [1.0])
        assert cache.get("あ") is None
        cache.save()


# --------------------------------------------------------------------------
# embed_texts — ミスだけ embed すること
# --------------------------------------------------------------------------

def _embed(texts, session, cache=None):
    return csr.embed_texts(
        texts, backend="ruri", batch_size=2, ollama_url="http://o",
        model="label", ruri_url="http://ruri:8000", session=session,
        sleeper=lambda *_: None, cache=cache,
    )


def test_embed_texts_without_cache_embeds_everything():
    s = _FakeSession()
    out = _embed(["a", "b", "c"], s)
    assert len(out) == 3
    assert sum(len(c) for c in s.embed_calls) == 3


def test_embed_texts_only_embeds_cache_misses(tmp_path):
    s = _FakeSession(vector=(9.0,))
    cache = csr.EmbeddingCache(tmp_path / "c.json", "m1")
    cache.put("a", [1.0])
    cache.put("c", [3.0])

    out = _embed(["a", "b", "c"], s, cache)

    assert out == [[1.0], [9.0], [3.0]]        # 順序が保たれること
    assert s.embed_calls == [["b"]]            # embed したのはミスの 1 件だけ
    assert cache.stats()["hits"] == 2
    assert cache.stats()["misses"] == 1


def test_embed_texts_preserves_order_with_all_misses():
    s = _FakeSession()
    out = _embed(["x", "y", "z", "w", "v"], s)
    assert len(out) == 5
    assert [t for call in s.embed_calls for t in call] == ["x", "y", "z", "w", "v"]


def test_embed_texts_stores_new_vectors_in_cache(tmp_path):
    s = _FakeSession(vector=(7.0,))
    cache = csr.EmbeddingCache(tmp_path / "c.json", "m1")
    _embed(["new"], s, cache)
    assert cache.get("new") == [7.0]


def test_open_embedding_cache_returns_none_without_path():
    assert csr.open_embedding_cache(
        None, backend="ruri", model="m", ruri_url="http://r", session=_FakeSession(),
    ) is None


def test_open_embedding_cache_returns_none_when_model_unresolvable(tmp_path):
    class Boom(_FakeSession):
        def get(self, url, timeout=None):
            raise csr.requests.ConnectionError("no route")

    assert csr.open_embedding_cache(
        tmp_path / "c.json", backend="ruri", model="m",
        ruri_url="http://r", session=Boom(),
    ) is None


# --------------------------------------------------------------------------
# 回帰: キャッシュを入れても結果が変わらないこと
# --------------------------------------------------------------------------
#
# これが一番大事なテスト。速くなっても出力が変わったら #3203 Phase 4 の
# cohort 比較 (v7 施行前後の分布) が壊れる。

def _write_articles(tmp_path, n=6):
    d = tmp_path / "articles"
    d.mkdir()
    for i in range(n):
        (d / f"2026-01-0{i+1}-B00000000{i}.json").write_text(
            json.dumps({
                "title": f"記事{i}",
                "slug": f"2026-01-0{i+1}-b00000000{i}",
                "product": {"name": f"商品{i}", "edu_domains": ["構成遊び"]},
                "tags": [f"タグ{i}"],
                "narrative": {"lead": f"リード文{i}"},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    return d


class _VaryingSession(_FakeSession):
    """テキストごとに違うベクトルを返す (全部同じだと差が出ず検出力が無い)。"""

    def post(self, url, json=None, timeout=None):
        texts = (json or {}).get("texts") or (json or {}).get("input") or []
        self.embed_calls.append(list(texts))
        vecs = [[float(len(t)), float(sum(map(ord, t)) % 97), 1.0] for t in texts]
        return _FakeResp({"vectors": vecs, "embeddings": vecs})


def test_cache_does_not_change_output(tmp_path):
    articles = _write_articles(tmp_path)
    cache_path = tmp_path / "embed_cache.json"

    cold_out = tmp_path / "cold.json"
    s1 = _VaryingSession()
    csr.run(articles_dir=articles, out_path=cold_out, backend="ruri",
            ruri_url="http://ruri:8000", session=s1, sleeper=lambda *_: None,
            embed_cache=cache_path)
    embedded_first = sum(len(c) for c in s1.embed_calls)

    warm_out = tmp_path / "warm.json"
    s2 = _VaryingSession()
    csr.run(articles_dir=articles, out_path=warm_out, backend="ruri",
            ruri_url="http://ruri:8000", session=s2, sleeper=lambda *_: None,
            embed_cache=cache_path)
    embedded_second = sum(len(c) for c in s2.embed_calls)

    cold = json.loads(cold_out.read_text(encoding="utf-8"))
    warm = json.loads(warm_out.read_text(encoding="utf-8"))
    # _meta には生成時刻が入るので、近傍の中身だけを比べる
    cold.pop("_meta", None)
    warm.pop("_meta", None)
    assert warm == cold

    assert embedded_first == 6      # 1 回目は全件
    assert embedded_second == 0     # 2 回目は 1 件も embed しない


def test_cache_reembeds_only_changed_article(tmp_path):
    articles = _write_articles(tmp_path)
    cache_path = tmp_path / "embed_cache.json"

    csr.run(articles_dir=articles, out_path=tmp_path / "a.json", backend="ruri",
            ruri_url="http://ruri:8000", session=_VaryingSession(),
            sleeper=lambda *_: None, embed_cache=cache_path)

    # 1 記事だけ本文を変える
    target = sorted(articles.glob("*.json"))[2]
    data = json.loads(target.read_text(encoding="utf-8"))
    data["narrative"]["lead"] = "書き換えたリード文"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    s = _VaryingSession()
    csr.run(articles_dir=articles, out_path=tmp_path / "b.json", backend="ruri",
            ruri_url="http://ruri:8000", session=s, sleeper=lambda *_: None,
            embed_cache=cache_path)
    assert sum(len(c) for c in s.embed_calls) == 1


def test_partial_run_does_not_prune_warm_cache(tmp_path):
    """--limit のスモーク 1 回で warm キャッシュを消さないこと。

    これを落とすと「スモークを回したせいで次の全件 run が丸ごと再計算」に
    なり、キャッシュを入れた意味が消える。
    """
    articles = _write_articles(tmp_path, n=6)
    cache_path = tmp_path / "c.json"

    csr.run(articles_dir=articles, out_path=tmp_path / "full.json", backend="ruri",
            ruri_url="http://r", session=_VaryingSession(), sleeper=lambda *_: None,
            embed_cache=cache_path)
    warm = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(warm["vectors"]) == 6

    # limit=2 の部分実行
    csr.run(articles_dir=articles, out_path=tmp_path / "part.json", backend="ruri",
            ruri_url="http://r", session=_VaryingSession(), sleeper=lambda *_: None,
            embed_cache=cache_path, limit=2)
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(after["vectors"]) == 6      # 削られていない

    # 次の全件 run は 1 件も embed しない
    s = _VaryingSession()
    csr.run(articles_dir=articles, out_path=tmp_path / "full2.json", backend="ruri",
            ruri_url="http://r", session=s, sleeper=lambda *_: None,
            embed_cache=cache_path)
    assert sum(len(c) for c in s.embed_calls) == 0


def test_full_run_still_prunes(tmp_path):
    articles = _write_articles(tmp_path, n=6)
    cache_path = tmp_path / "c.json"
    csr.run(articles_dir=articles, out_path=tmp_path / "a.json", backend="ruri",
            ruri_url="http://r", session=_VaryingSession(), sleeper=lambda *_: None,
            embed_cache=cache_path)

    # 記事を 2 本削る = そのベクタは次の全件 run で捨てられるべき
    for f in sorted(articles.glob("*.json"))[:2]:
        f.unlink()
    csr.run(articles_dir=articles, out_path=tmp_path / "b.json", backend="ruri",
            ruri_url="http://r", session=_VaryingSession(), sleeper=lambda *_: None,
            embed_cache=cache_path)
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(after["vectors"]) == 4
