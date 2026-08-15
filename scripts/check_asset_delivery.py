"""HTML が参照している指紋付きアセットが実際に配信されているかを外から見る (#5260)。

`check_delivery_freshness.py` (#5042) が「サイトの時刻が進んでいるか」を見るのに対し、
こちらは **「いま配信されている HTML と、その HTML が参照しているアセットが噛み合って
いるか」** を見る。両方 200 でも噛み合っていない状態がありうる:

    HTML: 新版 (新しい指紋の CSS を参照)
    CSS : まだ入れ替わっていない / もう消えた  → 404

## なぜこれが一過性で済まないか (2026-08-15 実測)

    $ curl -sSI https://navi.omcha.jp/assets/css/stylesheet.<存在しない指紋>.css
    HTTP/1.1 404 Not Found
    Cache-Control: max-age=31536000      ← **1 年**
    cf-cache-status: MISS                ← 2 回目は HIT (エッジにも載る)

    $ curl -sSI https://navi.omcha.jp/no-such-page/
    HTTP/1.1 404 Not Found
    Cache-Control: max-age=300           ← HTML の 404 は 5 分

つまり `/assets/**` と `/js/**` に効いている「指紋付きだから 1 年キャッシュしてよい」
という設定が、**200 だけでなく 404 にも適用されている**。デプロイ入れ替え窓の
一瞬の 404 を掴んだブラウザは、その 404 を 1 年間保持する。実体が配信された後も
同じ URL を取りに行かないため、リロードしても直らない (curl は 200 を返すのに
ブラウザだけスタイル無しのまま = #5260 の症状)。

GitLab Pages は世代を保持せず最新の artifact だけを配信するので、**古い HTML
(ブラウザ 10 分 / エッジ 1h) が新しいデプロイ後に消えた指紋 URL を引く**経路も同じ
結果になる。404 が出ること自体はこの構成では避けられない。避けられるのは
**その 404 を長期キャッシュさせないこと**で、そこが直っているかをここで見張る。

## 見るもの

- R1 `<link>` / `<script>` が参照する同一オリジンの指紋付きアセットが 200 か
- R2 その応答が HTML でないか (指紋 URL に 404 ページ本文が 200 で返る #3568 の再発)
- R3 プレーン取得が 404 なのにキャッシュバスター付きだと 200 = **エッジに 404 が
  焼き付いている**状態 (#5260 で報告されたそのもの)
- R4 存在しないアセット URL の 404 に長い `Cache-Control` が付いていないか
  (= 焼き付きの原因設定が戻っていないかの回帰ガード)

R4 だけは「異常が起きていなくても」効く。R1〜R3 はデプロイ窓に当たらないと鳴らない
一方、R4 は設定が戻った瞬間に鳴る。**この script の主目的は R4** で、R1〜R3 は
実害が出ているときに状況を添えるためのもの。

## 設計判断

- **原因非依存**。GitLab のパイプライン API を叩いて成否を判定しない (#5042 と同じ
  理由: 監視対象の CI に監視が依存する)。パイプラインを見たいときは人間が
  `scripts/gitlab_pipeline_status.py` を使う。
- **commit-back しない**。判定は毎回その場で取り直せる。
- **判定不能を ok に潰さない**。ページを取れないこと自体が異常。

起票規律 (CLAUDE.md: GitHub API バースト禁止):
  マーカー ``<!-- asset-delivery-monitor -->`` で単一 issue を特定して body を更新し、
  健全に戻ったら close する。1 run あたりの書き込みは最大 1 回。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_asset_delivery")

MARKER = "asset-delivery-monitor"
LABELS = "tech-debt,todo"
DEFAULT_PAGES = ["https://navi.omcha.jp/", "https://navi.omcha.jp/ranking/"]
DEFAULT_USER_AGENT = "navi-asset-delivery/1.0 (+https://navi.omcha.jp)"
DEFAULT_TIMEOUT = 30.0

# 404 に許す最大 max-age。HTML の 404 は実測 300 秒なので、それに揃える。
# ここを超えていたら「指紋付きだから 1 年」の設定が 404 にも当たっている。
DEFAULT_MAX_NEGATIVE_TTL = 300

# 1 ページから追うアセットの上限。head の指紋付き CSS/JS は数本なので、
# これを超えるのはテンプレート側の異常。監視が重くならないよう頭を押さえる。
MAX_ASSETS_PER_PAGE = 12

# 属性値はクォート有り / 無しの両方がある (Hugo の --minify はクォートを落とす)。
_TAG_RE = re.compile(r"<(?:link|script)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE
)
# Hugo の fingerprint は SHA-256 の 64 桁 hex を拡張子の直前に挟む。
_FINGERPRINT_RE = re.compile(r"\.[0-9a-f]{32,64}\.(?:css|js)$", re.IGNORECASE)


class Probe(NamedTuple):
    """1 リクエストの結果。テストではこれを直接組み立てて注入する。"""

    status: int
    headers: Dict[str, str]
    text: str

    def header(self, name: str) -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return ""


class FetchError(Exception):
    """ページ / アセットを取得できなかった (DNS / 接続 / タイムアウト)。"""


def extract_assets(html: str, base_url: str) -> List[str]:
    """HTML から同一オリジンの指紋付き CSS/JS の絶対 URL を重複なく返す。

    指紋付きに絞るのは、監視したいのが「内容不変を前提に長期キャッシュされる URL」
    だけだから。指紋の無い URL は入れ替わっても同じ URL のままなので、404 が
    焼き付いても次のデプロイで自然に復旧する。
    """
    origin = urlparse(base_url).netloc
    found: List[str] = []
    for tag in _TAG_RE.findall(html):
        for quoted, single, bare in _ATTR_RE.findall(tag):
            raw = quoted or single or bare
            if not raw:
                continue
            url = urljoin(base_url, raw.strip())
            if urlparse(url).netloc != origin:
                continue  # 別オリジン (gtag 等) は配信の責任範囲外
            if not _FINGERPRINT_RE.search(urlparse(url).path):
                continue
            if url not in found:
                found.append(url)
    return found[:MAX_ASSETS_PER_PAGE]


def missing_asset_url(assets: Sequence[str], base_url: str) -> str:
    """R4 用に「確実に存在しない」アセット URL を作る。

    実在するアセットと同じディレクトリ・同じ形 (指紋付き) にするのが要点。
    キャッシュ設定はパスにマッチさせて効かせるので、`/tmp/nope.txt` のような
    別形状の URL を投げても本番と同じルールに当たらず、回帰ガードにならない。
    """
    fake = "0" * 64
    if assets:
        path = urlparse(assets[0]).path
        directory, _, filename = path.rpartition("/")
        ext = filename.rsplit(".", 1)[-1]
        return urljoin(assets[0], f"{directory}/probe-404-{fake}.{ext}")
    return urljoin(base_url, f"/assets/css/probe-404-{fake}.css")


def parse_max_age(cache_control: str) -> Optional[int]:
    """Cache-Control から max-age 秒を読む。無ければ None。

    `no-store` / `no-cache` は「キャッシュするな」なので 0 として扱う
    (max-age が併記されていても、そちらが勝つ)。
    """
    if not cache_control:
        return None
    lowered = cache_control.lower()
    if "no-store" in lowered or "no-cache" in lowered:
        return 0
    match = re.search(r"\bmax-age\s*=\s*(\d+)", lowered)
    return int(match.group(1)) if match else None


def _is_html(probe: Probe) -> bool:
    return "text/html" in probe.header("content-type").lower()


def check_asset(url: str, fetch: Callable[[str], Probe]) -> Dict[str, Any]:
    """アセット 1 本を見る。status は ok / missing / edge_stale / html_body。"""
    try:
        probe = fetch(url)
    except FetchError as exc:
        return {"url": url, "status": "unreachable", "detail": str(exc)}

    if probe.status == 200:
        if _is_html(probe):
            # 指紋 URL に HTML が 200 で返る。nosniff により script は無音で
            # ブロックされ、機能だけが静かに死ぬ (#3568 で実測済み)。
            return {"url": url, "status": "html_body",
                    "detail": f"content-type={probe.header('content-type')}"}
        return {"url": url, "status": "ok", "detail": ""}

    # 非 200。キャッシュバスターを付けて取り直し、エッジ/ブラウザ側の焼き付きと
    # 「実体がそもそも無い」を切り分ける (#5260 の報告そのものの手口)。
    busted = f"{url}{'&' if '?' in url else '?'}cb=asset-delivery-monitor"
    try:
        retry = fetch(busted)
    except FetchError as exc:
        return {"url": url, "status": "missing",
                "detail": f"HTTP {probe.status} (再取得も失敗: {exc})"}

    if retry.status == 200 and not _is_html(retry):
        return {"url": url, "status": "edge_stale",
                "detail": f"プレーン HTTP {probe.status} / cb 付きは 200 "
                          f"(cf-cache-status={probe.header('cf-cache-status') or '-'})"}
    return {"url": url, "status": "missing", "detail": f"HTTP {probe.status}"}


def check_negative_ttl(url: str, fetch: Callable[[str], Probe],
                       max_ttl: int) -> Dict[str, Any]:
    """存在しないアセット URL の 404 に長期キャッシュが付いていないか (R4)。"""
    try:
        probe = fetch(url)
    except FetchError as exc:
        return {"url": url, "status": "unreachable", "ttl": None, "max_ttl": max_ttl,
                "detail": str(exc)}

    cache_control = probe.header("cache-control")
    ttl = parse_max_age(cache_control)

    if probe.status == 200:
        # 存在しないはずの URL が 200。指紋 URL に fallback を返す設定になっている。
        return {"url": url, "status": "unexpected_200", "ttl": ttl, "max_ttl": max_ttl,
                "detail": f"content-type={probe.header('content-type')}"}
    if ttl is not None and ttl > max_ttl:
        return {"url": url, "status": "sticky_404", "ttl": ttl, "max_ttl": max_ttl,
                "detail": f"HTTP {probe.status} / Cache-Control: {cache_control}"}
    return {"url": url, "status": "ok", "ttl": ttl, "max_ttl": max_ttl,
            "detail": f"HTTP {probe.status} / Cache-Control: {cache_control or '(無し)'}"}


def check(pages: Sequence[str], fetch: Callable[[str], Probe],
          max_negative_ttl: int = DEFAULT_MAX_NEGATIVE_TTL) -> Dict[str, Any]:
    """全ページ分を 1 件に畳む。status は ok / broken / unreachable。"""
    rows: List[Dict[str, Any]] = []
    assets_seen: List[str] = []
    page_errors: List[Dict[str, Any]] = []

    for page in pages:
        try:
            probe = fetch(page)
        except FetchError as exc:
            page_errors.append({"url": page, "status": "unreachable", "detail": str(exc)})
            continue
        if probe.status != 200:
            page_errors.append({"url": page, "status": "unreachable",
                                "detail": f"HTTP {probe.status}"})
            continue
        assets = extract_assets(probe.text, page)
        if not assets:
            # 指紋付きアセットが 1 本も無い HTML は、テンプレートが壊れたか
            # 取得したものが HTML ではない。ok に潰さない。
            page_errors.append({"url": page, "status": "no_assets",
                                "detail": "指紋付きアセットの参照が 1 件も無い"})
            continue
        for asset in assets:
            if asset in assets_seen:
                continue  # ページ間で共有される stylesheet を二度突かない
            assets_seen.append(asset)
            rows.append(dict(check_asset(asset, fetch), page=page))

    negative = check_negative_ttl(
        missing_asset_url(assets_seen, pages[0] if pages else ""), fetch, max_negative_ttl
    )

    bad_assets = [r for r in rows if r["status"] != "ok"]
    if page_errors:
        status = "unreachable"
    elif bad_assets or negative["status"] not in ("ok",):
        status = "broken"
    else:
        status = "ok"

    return {
        "status": status,
        "pages": list(pages),
        "assets": rows,
        "page_errors": page_errors,
        "negative": negative,
    }


def render_body(result: Dict[str, Any], now: dt.datetime) -> str:
    headline = {
        "broken": "HTML が参照しているアセットが配信されていません",
        "unreachable": "監視対象ページを取得できません",
    }.get(result["status"], "アセット配信に異常があります")

    parts = [
        f"<!-- {MARKER} -->",
        "",
        f"**{headline}** ({now.isoformat()} 時点)。",
        "",
    ]

    if result["page_errors"]:
        parts += ["## ページ取得", "", "| URL | 状態 | 詳細 |", "| --- | --- | --- |"]
        for row in result["page_errors"]:
            parts.append(f"| {row['url']} | `{row['status']}` | {row['detail']} |")
        parts.append("")

    if result["assets"]:
        parts += ["## 参照アセット", "", "| アセット | 状態 | 詳細 |", "| --- | --- | --- |"]
        for row in result["assets"]:
            parts.append(f"| `{urlparse(row['url']).path}` | `{row['status']}` | {row['detail']} |")
        parts.append("")

    neg = result["negative"]
    parts += [
        "## 404 の Cache-Control (焼き付きの回帰ガード)",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| 状態 | `{neg['status']}` |",
        f"| 探索 URL | `{urlparse(neg['url']).path}` |",
        f"| max-age | {'-' if neg['ttl'] is None else neg['ttl']} |",
        f"| 上限 | {neg['max_ttl']} |",
        f"| 詳細 | {neg['detail']} |",
        "",
        "## 見かた",
        "",
        "- `sticky_404` — **これが本丸**。存在しないアセット URL の 404 に長い "
        "`Cache-Control` が付いている。デプロイ入れ替え窓に 404 を掴んだブラウザは "
        "その期間ずっとスタイル無しのままになる (curl は 200 なのにブラウザだけ壊れる)。"
        "Cloudflare 側の `/assets/**` `/js/**` 向けキャッシュ設定が 404 にも "
        "適用されていないか確認すること (#5260)",
        "- `edge_stale` — プレーン取得は 404 だがキャッシュバスター付きなら 200。"
        "エッジに 404 が載っている。実体はあるので、purge すれば直る",
        "- `missing` — 実体が無い。デプロイ途中か、ビルド生成物が不完全",
        "- `html_body` — 指紋 URL に HTML が 200 で返っている (#3568 の再発)。"
        "`nosniff` により script は無音でブロックされる",
        "- `unreachable` / `no_assets` — サイト自体かテンプレートの異常",
        "",
        "配信パイプラインの現況は GitLab 側にしか無い。手元から見るには:",
        "",
        "```bash",
        "GITLAB_TOKEN=<PAT> python scripts/gitlab_pipeline_status.py",
        "```",
        "",
        f"- マーカー `<!-- {MARKER} -->` で同一 open Issue を特定し、body を毎回更新します",
        "- 配信が健全に戻ったら自動 close します",
        "",
        "Refs #5260, #5042, #3568",
    ]
    return "\n".join(parts)


# --- 取得 -------------------------------------------------------------------

def http_fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
               user_agent: str = DEFAULT_USER_AGENT) -> Probe:
    """1 本取得する。例外は FetchError に畳み、非 200 は Probe として返す。

    非 200 を例外にしないのは、この監視では **404 のヘッダそのもの**が判定材料だから。
    UA を明示するのは素の User-Agent だと 403 が返るため (#5042 で実測)。
    """
    import requests  # 遅延 import: テストは fetch を差し替えるので requests 不要

    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
    except Exception as exc:  # requests の例外階層に依存しない
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    return Probe(status=resp.status_code, headers=dict(resp.headers), text=resp.text)


# --- GitHub 側 (check_delivery_freshness.py と同じ作法) ----------------------

def _gh(args: List[str]) -> str:
    import subprocess
    res = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return res.stdout


def get_open_issue(repo: str) -> Optional[int]:
    query = f'repo:{repo} is:issue is:open in:body "{MARKER}"'
    out = _gh(["api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=10"])
    items = json.loads(out).get("items", [])
    return items[0]["number"] if items else None


def create_issue(repo: str, title: str, body: str) -> str:
    return _gh(["issue", "create", "-R", repo, "--title", title,
                "--label", LABELS, "--body", body]).strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    return _gh(["issue", "edit", str(number), "-R", repo,
                "--title", title, "--body", body]).strip()


def close_issue(repo: str, number: int) -> None:
    _gh(["issue", "close", str(number), "-R", repo,
         "--comment", "アセット配信が健全に戻ったため自動 close します (#5260)。"])


def title_for(result: Dict[str, Any]) -> str:
    if result["status"] == "unreachable":
        return "[delivery] 監視対象ページを取得できません"
    if result["negative"]["status"] == "sticky_404":
        return "[delivery] アセットの 404 が長期キャッシュされる設定になっています"
    return "[delivery] HTML が参照しているアセットが配信されていません"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--page", action="append", dest="pages", default=None,
                   help="監視対象ページ (繰り返し指定可, 既定: トップと /ranking/)")
    p.add_argument("--max-negative-ttl", type=int, default=DEFAULT_MAX_NEGATIVE_TTL,
                   help="404 に許す最大 max-age 秒 (既定 300)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--now", default=None, help="判定基準時刻 (ISO8601)。replay 検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="issue を触らず判定結果と body だけ出す")
    args = p.parse_args(argv)

    now = (dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))
    pages = args.pages or DEFAULT_PAGES

    result = check(pages, fetch=lambda u: http_fetch(u, timeout=args.timeout),
                   max_negative_ttl=args.max_negative_ttl)
    logger.info("status=%s assets=%d bad=%d negative=%s(ttl=%s)",
                result["status"], len(result["assets"]),
                len([r for r in result["assets"] if r["status"] != "ok"]),
                result["negative"]["status"], result["negative"]["ttl"])

    healthy = result["status"] == "ok"
    body = render_body(result, now)

    if args.dry_run:
        print(body if not healthy else "healthy — 起票しません")
        return 0

    if not args.repo:
        logger.error("--repo (または環境変数 REPO) が要ります")
        return 2

    existing = get_open_issue(args.repo)
    if healthy:
        if existing:
            close_issue(args.repo, existing)
            logger.info("closed #%d", existing)
        else:
            logger.info("healthy — 何もしません")
        return 0

    title = title_for(result)
    if existing:
        logger.info("updated %s", update_issue(args.repo, existing, title, body))
    else:
        logger.info("created %s", create_issue(args.repo, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
