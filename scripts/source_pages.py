"""出典候補のページを取得し、リンク切れの判定と短い抜粋を作る (navi-brain#117)。

使い道は 2 つ:

- リンク切れの記録 (check_third_party_links.py): 事前収集の候補 URL を 1 回ずつ開き、
  ``third_party_sources.json`` の各行に ``link`` を書く。生成前の判定
  (score_per_asin_info) は ``dead`` の候補を数えない。
- 抜粋 (build_jules_prompt の sources 注記): 03 がプロンプトを組むときに候補を開き、
  商品名・型番の語の周りを短く切り出して Jules に渡す。Jules の閲覧ツールで開けない
  ページでも、抜粋を根拠に採用できるようにする (不合格の型 B)。

**dead にするのは確実なときだけ。** HTTP 404 / 410 と、ホスト名が引けない (DNS) ときだけ。
403・5xx・タイムアウト・接続断は ``unknown`` (取得元の IP や混み具合で変わる。Actions の
共有 IP は 403 を返されやすい)。unknown は数え続ける。段階 0 の測定では、合格記事が
実際に参照した候補 URL のうち「取得できなかった」のは 3% で、それをさらに 404/DNS に
絞っている。

**本文は repo に残さない。** public リポジトリなので、第三者のページ本文・抜粋は commit
しない。``link`` に書くのは状態と HTTP コードと日付だけ。抜粋はプロンプトにだけ入れる。
"""

from __future__ import annotations

import html
import re
import socket
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

_UA = (
    "Mozilla/5.0 (compatible; omochairo-source-check/1.0; "
    "+https://navi.omcha.jp/)"
)
_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# DNS で引けない (requests は NameResolutionError / getaddrinfo の文言で包む)
_DNS_FAIL_RE = re.compile(
    r"NameResolutionError|Name or service not known|getaddrinfo failed|"
    r"nodename nor servname|No address associated|Temporary failure in name resolution",
    re.IGNORECASE,
)
# Temporary failure は一時的な DNS 障害なので dead にしない
_DNS_TEMPORARY_RE = re.compile(r"Temporary failure in name resolution", re.IGNORECASE)

DEAD_HTTP = (404, 410)
# 名前が存在しない: EAI_NONAME (Linux -2 / macOS 8) と Windows の WSAHOST_NOT_FOUND (11001)
_DNS_NOT_FOUND_ERRNOS = {getattr(socket, "EAI_NONAME", -2), 11001}
MAX_BYTES = 1_500_000


@dataclass
class Page:
    status: str          # "ok" / "dead" / "unknown"
    http: int | None     # HTTP ステータス (取れなかったら None)
    text: str = ""       # タグを剥がした本文 (ok のときだけ)
    reason: str = ""     # unknown / dead の理由 (例外名など)


def html_to_text(raw: str) -> str:
    raw = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", raw)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def classify_exception(exc: BaseException) -> str:
    """取得の例外を dead / unknown に分ける。DNS で引けないときだけ dead。"""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, socket.gaierror):
        # 「その名前は無い」と言い切るものだけ dead。EAI_AGAIN (一時障害) やその他は unknown。
        # DNS 全体の障害で一斉に dead になるのは check_third_party_links が別に止める
        return "dead" if reason.errno in _DNS_NOT_FOUND_ERRNOS else "unknown"
    msg = f"{type(exc).__name__}: {exc} {reason or ''}"
    if _DNS_FAIL_RE.search(msg) and not _DNS_TEMPORARY_RE.search(msg):
        return "dead"
    return "unknown"


_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", re.IGNORECASE)


def _decode(raw: bytes, header_charset: str | None) -> str:
    enc = header_charset
    if not enc:
        m = _CHARSET_RE.search(raw[:4096])
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def fetch_page(url: str, timeout: float = 8.0) -> Page:
    """URL を 1 回開く。例外は投げない。

    標準ライブラリだけで書く: 03-invoke-jules は pip install をしないジョブで呼ぶため。
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept-Language": "ja,en;q=0.5"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 収集済みの候補 URL
            code = getattr(r, "status", None) or r.getcode()
            ct = (r.headers.get("Content-Type") or "").lower()
            if ct and "html" not in ct and "text" not in ct:
                return Page("ok", code)
            raw = r.read(MAX_BYTES)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                # MAX_BYTES で切れた gzip も、読めたところまで展開する
                raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
            body = _decode(raw, r.headers.get_content_charset())
            return Page("ok", code, html_to_text(body))
    except urllib.error.HTTPError as e:
        return Page("dead" if e.code in DEAD_HTTP else "unknown", e.code)
    except Exception as e:  # noqa: BLE001 — 取得の失敗はすべて状態に倒す
        return Page(classify_exception(e), None, reason=type(e).__name__)


# Amazon の商品名に多い SEO 語 (段階 0: これが混ざると語の一致率が下がる)。抜粋の起点にしない
_STOP_TOKENS = {
    "おもちゃ", "オモチャ", "知育玩具", "知育", "玩具", "プレゼント", "ギフト", "クリスマス",
    "誕生日", "室内遊び", "まとめ買い", "セット", "男の子", "女の子", "子供", "子ども",
    "こども", "キッズ", "ベビー", "対象年齢", "人気", "おすすめ", "送料無料", "正規品",
    "日本語説明書", "新品", "公式", "以上", "向け", "歳から", "サイズ", "カラー", "タイプ",
    "モデル", "シリーズ", "入学祝い", "出産祝い", "クリスマスプレゼント", "誕生日プレゼント",
}
_JAN_RE = re.compile(r"(?<!\d)(?:45|49)\d{11}(?!\d)")
# 英字と数字が混ざった型番 (例: 33506 は数字だけなので別に拾う、BRIO-33506、TRC-013204)
_MODEL_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{3,}(?![A-Za-z0-9])")
_DIGITS_RE = re.compile(r"(?<!\d)\d{5,8}(?!\d)")
_WORD_RE = re.compile(r"[ァ-ヴー]{3,}|[一-龥々]{2,}[ァ-ヴー一-龥々]*|[A-Za-z][A-Za-z'&]{3,}")


def identity_tokens(title: str, extra: tuple[str, ...] = ()) -> list[str]:
    """商品名から、本文で商品を指していそうな語を強い順に返す (型番・JAN → 語)。"""
    title = title or ""
    out: list[str] = []

    def add(t: str) -> None:
        t = t.strip("-")
        if len(t) >= 2 and t not in out and t not in _STOP_TOKENS:
            out.append(t)

    for t in extra:
        if t:
            add(str(t))
    for m in _JAN_RE.findall(title):
        add(m)
    for m in _MODEL_RE.findall(title):
        if not m.isalpha():
            add(m)
    for m in _DIGITS_RE.findall(title):
        add(m)
    words = [w for w in _WORD_RE.findall(title) if w not in _STOP_TOKENS]
    # 長い語ほど商品固有 (ブランド名・通称) のことが多い。先頭 (ブランド) は残す
    head = words[:1]
    rest = sorted(words[1:], key=len, reverse=True)
    for w in head + rest:
        add(w)
    return out[:8]


_HEAD_SKIP = 300  # 先頭 (title・メニュー) は、本文に別の出現があれば抜粋の起点にしない
_MAX_OCCURRENCES = 30


def excerpt(text: str, tokens: list[str], width: int = 70, limit: int = 140) -> tuple[str, list[str]]:
    """本文から、商品の語がいちばん集まっている箇所の前後を切り出す。(抜粋, 本文に出た語)。

    語が 1 つも出なければ ("", [])。抜粋は limit 字以内 (public でないプロンプトに入れる
    だけだが、第三者の文章なので長く写さない)。ページ先頭の title・メニューは、本文に
    別の出現があれば使わない (先頭だと「MENU おすすめ…」のようなナビが抜粋になる)。
    """
    if not text:
        return "", []
    hits = [t for t in tokens if t in text]
    if not hits:
        return "", []
    positions = []
    for t in hits:
        start = 0
        for _ in range(_MAX_OCCURRENCES):
            p = text.find(t, start)
            if p < 0:
                break
            positions.append((p, t))
            start = p + len(t)
    body = [x for x in positions if x[0] >= _HEAD_SKIP] or positions

    def score(pt):
        p, t = pt
        window = text[max(0, p - width): p + len(t) + width]
        return (sum(1 for h in hits if h in window), -p)

    pos, tok = max(body, key=score)
    start = max(0, pos - width)
    end = min(len(text), pos + len(tok) + width)
    snip = text[start:end].strip()
    if len(snip) > limit:
        snip = snip[:limit]
    return ("…" if start > 0 else "") + snip + ("…" if end < len(text) else ""), hits


# 商品名に出るが、その商品を指すとは言えない分類語 (一致しても「商品を確かめた」にしない)
_GENERIC_TOKENS = {
    "カードゲーム", "ボードゲーム", "パズル", "知育パズル", "ジグソーパズル", "ブロック", "ぬいぐるみ",
    "英語", "絵本", "図鑑", "モンテッソーリ", "論理的思考力", "プログラミング", "ミニカー",
    "Games", "Game", "Time", "Toys", "Toy", "Kids", "Baby", "Puzzle", "Block", "Blocks",
}


def strong_match(hits: list[str]) -> bool:
    """本文の一致が「この商品の話」と言える強さか。

    型番・JAN (数字を含む語) か、分類語でない 6 字以上の語 (ブランド名・商品の通称) が
    出たとき、または分類語を除いて 3 語以上出たとき。段階 0 の測定で、語 1 つの一致は
    別の商品のページ (型 C) でもよく起きる。
    """
    specific = [h for h in hits if h not in _GENERIC_TOKENS]
    if any(any(c.isdigit() for c in h) for h in specific):
        return True
    if any(len(h) >= 6 for h in specific):
        return True
    return len(set(specific)) >= 3
