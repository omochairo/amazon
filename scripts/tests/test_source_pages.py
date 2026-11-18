"""source_pages (navi-brain#117): リンク切れの判定・本文化・抜粋。"""

import http.server
import socket
import threading
import urllib.error

import pytest

import source_pages as sp


def test_html_to_text_strips_scripts_and_entities():
    raw = "<html><script>var x=1</script><style>.a{}</style><p>ロジカル&amp;ルート</p>\n<b>パズル</b></html>"
    assert sp.html_to_text(raw) == "ロジカル&ルート パズル"


def test_identity_tokens_puts_model_and_jan_first_and_drops_seo_words():
    toks = sp.identity_tokens("BRIO ブリオ バッテリーパワートラベルトレイン 33506 知育玩具 プレゼント", ("4005556335069",))
    assert toks[0] == "4005556335069"
    assert "33506" in toks
    assert "知育玩具" not in toks and "プレゼント" not in toks
    assert "バッテリーパワートラベルトレイン" in toks


def test_excerpt_cuts_around_first_hit_within_limit():
    text = "あ" * 300 + "ロジカルルートパズルは4歳から遊べる" + "い" * 300
    snip, hits = sp.excerpt(text, ["存在しない語", "ロジカルルートパズル"])
    assert hits == ["ロジカルルートパズル"]
    assert "ロジカルルートパズル" in snip
    assert len(snip) <= 142  # limit + 前後の「…」
    assert snip.startswith("…") and snip.endswith("…")


def test_excerpt_prefers_body_over_page_title_and_menu():
    text = "ロジカルルートパズル | サイト名 MENU おすすめ 一覧" + "x" * 400 + \
           "くもんのロジカルルートパズルは、論理的に道をつなぐ知育パズルです。" + "y" * 50
    snip, _ = sp.excerpt(text, ["ロジカルルートパズル", "知育パズル"])
    assert "MENU" not in snip and "道をつなぐ" in snip


def test_strong_match_needs_model_or_specific_name():
    assert sp.strong_match(["33506"])                       # 型番
    assert sp.strong_match(["ロジカルルートパズル"])          # 6 字以上の通称
    assert not sp.strong_match(["Games", "Time"])            # 分類語だけ
    assert not sp.strong_match(["カードゲーム", "Kav's"])     # 分類語 + 短い語 1 つ
    assert sp.strong_match(["Kav's", "Sharks", "ELT"]) is True  # 分類語を除いて 3 語


def test_excerpt_empty_when_no_token_in_text():
    assert sp.excerpt("まったく別の話", ["ロジカルルートパズル"]) == ("", [])
    assert sp.excerpt("", ["x"]) == ("", [])


def test_classify_exception_dead_only_for_permanent_dns_failure():
    nx = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
    assert sp.classify_exception(nx) == "dead"
    temp = urllib.error.URLError(socket.gaierror(getattr(socket, "EAI_AGAIN", -3), "Temporary failure"))
    assert sp.classify_exception(temp) == "unknown"
    assert sp.classify_exception(urllib.error.URLError(ConnectionRefusedError())) == "unknown"
    assert sp.classify_exception(TimeoutError()) == "unknown"
    # Windows の WSAHOST_NOT_FOUND は dead、それ以外の gaierror は unknown
    assert sp.classify_exception(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))) == "dead"
    assert sp.classify_exception(urllib.error.URLError(socket.gaierror(11002, "try again"))) == "unknown"


def test_fetch_page_never_raises_on_malformed_url():
    assert sp.fetch_page("not a url", timeout=1).status == "unknown"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = {"/ok": 200, "/gone": 410, "/missing": 404, "/forbidden": 403, "/err": 503,
                "/shop": 200}[self.path]
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=shift_jis")
        self.end_headers()
        body = "<p>くもん ロジカルルートパズル</p>"
        if self.path == "/shop":
            body += "<p>1,430円 (税込)</p><button>カートに入れる</button>"
        self.wfile.write(body.encode("shift_jis"))

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_page_statuses(server):
    ok = sp.fetch_page(server + "/ok", timeout=5)
    assert ok.status == "ok" and ok.http == 200
    assert "ロジカルルートパズル" in ok.text  # charset ヘッダで復号する
    assert ok.shop is False
    shop = sp.fetch_page(server + "/shop", timeout=5)
    assert (shop.status, shop.shop) == ("ok", True)  # 開けたうえで通販の印が付く
    assert sp.fetch_page(server + "/missing", timeout=5).status == "dead"
    assert sp.fetch_page(server + "/gone", timeout=5).status == "dead"
    # 403 / 5xx は取得元で変わるので dead にしない
    forbidden = sp.fetch_page(server + "/forbidden", timeout=5)
    assert (forbidden.status, forbidden.http) == ("unknown", 403)
    assert sp.fetch_page(server + "/err", timeout=5).status == "unknown"


# ---- navi-brain#170 E1: 通販・卸の商品ページらしさ

def test_looks_like_shop_cart_button_with_price():
    text = "トミカ No.1 日産 スカイライン 550円 (税込) 数量 1 カートに入れる お気に入りに追加"
    assert sp.looks_like_shop("<html></html>", text)
    # 海外の玩具店 (ポーランド語・英語)
    assert sp.looks_like_shop("", "Klocki 129,99 zł Dodaj do koszyka")
    assert sp.looks_like_shop("", "Plarail S-13 $24.99 Add to Cart")


def test_looks_like_shop_price_meta_or_og_product_with_offer():
    # Shopify などはカートを JS で描くので、本文にボタンの文言が無い
    assert sp.looks_like_shop('<meta property="product:price:amount" content="3480">', "商品説明")
    both = '<meta property="og:type" content="product"><script>{"@type": "Offer", "price": 1}</script>'
    assert sp.looks_like_shop(both, "説明だけ")


def test_looks_like_shop_ignores_articles_and_maker_pages():
    # 記事の文中の「カートに入れる前に」「カゴに入れるのも」はボタンではない
    assert not sp.looks_like_shop("", "通販は届くまでの日数が違います。カートに入れる前に確かめる。価格 1,980円")
    assert not sp.looks_like_shop("", "果物をつまんでカゴに入れるのも楽しい。1,980円")
    # メーカーの製品紹介 (og:type=product だけ) と、レビュー記事の構造化データ (Offer だけ)
    assert not sp.looks_like_shop('<meta property="og:type" content="product">', "希望小売価格 3,300円")
    assert not sp.looks_like_shop('<script>{"@type":"Offer"}</script>', "レビュー 価格 3,300円")
    # ボタンの文言はあっても、価格も商品のメタも無い (ヘッダーのカートのリンク等)
    assert not sp.looks_like_shop("", "ログイン カートに入れる 商品一覧")


def test_looks_like_shop_edge_cases_from_review():
    # 記事の文 (「かごに入れる遊び」「かごに入れることができる」) は数えない
    assert not sp.looks_like_shop("", "お買い物ごっこで果物をかごに入れる遊び。1,980円")
    assert not sp.looks_like_shop("", "おもちゃをカゴに入れることができる。2,980円")
    # 本文化で消える input / img のボタンも拾う
    assert sp.looks_like_shop('<input type="submit" value="カートに入れる">', "価格 1,430円")
    assert sp.looks_like_shop('<button><img alt="買い物かごに入れる"></button>', "1,430円")
    # 整数の外貨・ポンド
    assert sp.looks_like_shop("", "Klocki 129 zł Dodaj do koszyka")
    assert sp.looks_like_shop("", "£19 Add to basket")
    # 価格メタは <meta> の属性だけ。記事本文に語が出ただけでは数えない
    assert not sp.looks_like_shop("<p>product:price:amount の解説</p>", "解説")
    # og:type が products など別の値なら商品ページにしない
    assert not sp.looks_like_shop('<meta property="og:type" content="products.list">'
                                  '<script>{"@type": "Offer"}</script>', "一覧")
