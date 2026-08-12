"""公開サイトが更新され続けているかを外側から 1 本で見る (#5042)。

判定材料は ``https://navi.omcha.jp/sitemap.xml`` の ``<lastmod>`` の最大値だけ。
Hugo はこれを **その URL を最後に動かしたコミットの時刻**として出すので、
「いまサイトに乗っているのはどのコミットか」を外から読める唯一の値になる。
これが古ければ、原因が何であれ配信が止まっている。

なぜ必要か:
  配信は GitHub と GitLab をまたいで成立している。

      main へ push → 40-mirror-to-gitlab.yml が git push
                   → GitLab CI の pages ジョブ → navi.omcha.jp

  GitHub 側の責務は ``git push`` が成功するところで終わっており、GitLab の
  パイプライン結果を見に行くコードはリポジトリ内に無い。したがって **GitLab 側の
  pages が落ちても GitHub の run は全部緑のまま、Issue も立たず、記事だけが
  増え続けてサイトが凍る**。

  #4789 (check_history_freshness.py) が「run が緑のまま止まる 3 経路」に対して
  原因非依存の網を張ったのと同じ話で、そこに 4 本目の経路 ——
  **別 CI に投げっぱなしで結果を見ていない** —— が空いていた。

なぜ既存の道具で代替できないか:
  ``audit_site_health.py`` は sitemap 掲載 URL の 200 / noindex / 孤立を見る。
  これは「配信された後のサイト」の健全性を見る道具で、配信そのものが止まると
  **監視対象の sitemap ごと古い状態で固まる**ため R1〜R5 はどれも鳴らない
  (古いページは古いまま 200 を返す)。止まったことを検出できるのは、
  「中身が正しいか」ではなく「時刻が進んでいるか」を見る網だけ。

設計判断:
- **原因非依存**。GitLab のパイプライン API を叩いて成否を見る作りにしない。
  それだと「GitLab は成功したが DNS / Pages 配信側で止まった」を取りこぼすし、
  監視対象の CI に監視が依存する。外から見えた事実 1 つで判定する。
- **監視は配信と別のインフラに置く**。この script は GitHub Actions 側で走らせる。
  GitLab 側に置くと GitLab が死んだとき監視も一緒に死ぬ。
- **commit-back しない**。履歴 JSONL に書き戻すと #4793 の
  「commit-back 全体を覆う continue-on-error」を新しく 1 本増やすことになり、
  しかもその新レーン自体が check_history_freshness.py の監視対象漏れになる。
  判定は毎回その場で取り直せるので状態を持つ必要が無い。
- **判定不能を ok に潰さない**。取得失敗も lastmod ゼロ件も、それ自体が異常。

起票規律 (CLAUDE.md: GitHub API バースト禁止):
  マーカー ``<!-- delivery-freshness-monitor -->`` で単一 issue を特定して body を
  更新し、健全に戻ったら close する。1 run あたりの書き込みは最大 1 回。

較正:
  2026-08-12 14:00 UTC 実測で、sitemap の最大 lastmod は同日 13:48 UTC ——
  **その 12 分前に main へマージされたコミットの時刻**だった (配信ラグは分単位)。
  記事マージは 25〜47 件/日あり配信は事実上毎日走るので、既定閾値 36h は
  導入日に鳴らない = 鳴ったら必ず異常。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_delivery_freshness")

MARKER = "delivery-freshness-monitor"
LABELS = "tech-debt,todo"
DEFAULT_SITEMAP = "https://navi.omcha.jp/sitemap.xml"
DEFAULT_MAX_AGE_HOURS = 36
DEFAULT_USER_AGENT = "navi-delivery-freshness/1.0 (+https://navi.omcha.jp)"
DEFAULT_TIMEOUT = 30.0

# sitemapindex を辿る深さの上限。循環参照した sitemap を掴んでも止まるように。
MAX_SITEMAP_DEPTH = 3

_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)


class FetchError(Exception):
    """sitemap を取得できなかった (DNS / 接続 / 非 200 / タイムアウト)。"""


def parse_lastmods(xml: str) -> List[dt.datetime]:
    """XML 中の ``<lastmod>`` を aware な datetime のリストにして返す。

    正規表現で拾うのは意図的。ここは freshness の網であって sitemap の
    スキーマ検証ではないので、名前空間宣言の揺れや部分的に壊れた XML でも
    「読めた分だけ」で判定を続けたい。読めない値は黙って捨てる。
    """
    found: List[dt.datetime] = []
    for raw in _LASTMOD_RE.findall(xml):
        value = raw.strip()
        # Hugo は +00:00 形式で出すが、Z 終端の sitemap も仕様上ありうる。
        if value.endswith(("Z", "z")):
            value = value[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # tz 無しの lastmod は UTC とみなす。ここで捨てると、tz を落とす
            # 実装に変わった瞬間に「lastmod ゼロ件」で誤報する。
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        found.append(parsed.astimezone(dt.timezone.utc))
    return found


def is_sitemap_index(xml: str) -> bool:
    return "<sitemapindex" in xml.lower()


def child_sitemaps(xml: str) -> List[str]:
    return [loc.strip() for loc in _LOC_RE.findall(xml)]


def latest_lastmod(url: str, fetch: Callable[[str], str],
                   depth: int = 0) -> Optional[dt.datetime]:
    """sitemap (必要なら子 sitemap も) を辿って最大の lastmod を返す。

    sitemapindex の場合、index 自身が持つ lastmod と子 sitemap の lastmod の
    両方を見る。現在の Hugo 出力は単一 urlset だが、URL 数が増えて分割された
    ときに黙って判定不能へ落ちないように最初から辿れるようにしておく。
    """
    xml = fetch(url)
    found = parse_lastmods(xml)
    if is_sitemap_index(xml) and depth < MAX_SITEMAP_DEPTH:
        for child in child_sitemaps(xml):
            if child == url:
                continue
            try:
                child_latest = latest_lastmod(child, fetch, depth + 1)
            except FetchError as exc:
                # 子 1 本が落ちても index 全体を判定不能にしない。
                logger.warning("child sitemap fetch failed: %s (%s)", child, exc)
                continue
            if child_latest is not None:
                found.append(child_latest)
    return max(found) if found else None


def check(url: str, now: dt.datetime, fetch: Callable[[str], str],
          max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> Dict[str, Any]:
    """配信の状態を 1 件返す。status は ok / stale / unreachable / unknown。"""
    try:
        latest = latest_lastmod(url, fetch)
    except FetchError as exc:
        # 取得できないこと自体が最重度の異常 (サイトが落ちている / DNS /
        # 証明書失効)。ここを ok に潰さない。
        return {"status": "unreachable", "url": url, "latest": None,
                "age_hours": None, "max_age_hours": max_age_hours,
                "detail": str(exc)}
    if latest is None:
        # 200 は返るが lastmod が 1 つも読めない = 空 sitemap か壊れた出力。
        return {"status": "unknown", "url": url, "latest": None,
                "age_hours": None, "max_age_hours": max_age_hours,
                "detail": "sitemap に読める <lastmod> が 1 件も無い"}
    age_hours = (now - latest).total_seconds() / 3600.0
    return {
        "status": "stale" if age_hours > max_age_hours else "ok",
        "url": url,
        "latest": latest.isoformat(),
        "age_hours": round(age_hours, 1),
        "max_age_hours": max_age_hours,
        "detail": "",
    }


def render_body(row: Dict[str, Any], now: dt.datetime) -> str:
    age = "-" if row["age_hours"] is None else f"{row['age_hours']}h"
    headline = {
        "stale": "サイトの更新が止まっています",
        "unreachable": "サイトに到達できません",
        "unknown": "sitemap から更新時刻を読めません",
    }.get(row["status"], "配信に異常があります")
    parts = [
        f"<!-- {MARKER} -->",
        "",
        f"**{headline}** ({now.isoformat()} 時点)。",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| 状態 | `{row['status']}` |",
        f"| sitemap | {row['url']} |",
        f"| 最新 lastmod | {row['latest'] or '-'} |",
        f"| 経過 | {age} |",
        f"| 上限 | {row['max_age_hours']}h |",
    ]
    if row.get("detail"):
        parts.append(f"| 詳細 | {row['detail']} |")
    parts += [
        "",
        "## なぜこれが GitHub 側で緑のまま起きるか",
        "",
        "配信は 2 つの CI をまたいでいます。",
        "",
        "```",
        "main へ push → 40-mirror-to-gitlab.yml が git push",
        "             → GitLab CI の pages ジョブ → navi.omcha.jp",
        "```",
        "",
        "GitHub 側の責務は `git push` が成功するところで終わりです。"
        "GitLab の pages が落ちても GitHub の run は全部緑のままなので、"
        "**記事だけが増え続けてサイトが凍ります**。この issue は"
        "「時刻が進んでいるか」だけを外から見る原因非依存の網です (#5042)。",
        "",
        "## 見かた",
        "",
        "- `stale` — サイトは生きているが更新が止まっている。"
        "GitLab 側の pages パイプラインと、`40-mirror-to-gitlab.yml` の直近 run "
        "(非 fast-forward で reject されていないか) を見ること",
        "- `unreachable` — sitemap を取得できない。サイト自体の停止 / DNS / 証明書を疑う "
        "(証明書は `41-pages-cert-renew.yml`)",
        "- `unknown` — 200 は返るが `<lastmod>` が読めない。ビルドは通ったが"
        "生成物が壊れている可能性",
        "",
        f"- マーカー `<!-- {MARKER} -->` で同一 open Issue を特定し、body を毎回更新します",
        "- 配信が健全に戻ったら自動 close します",
        "",
        "Refs #5042, #4789, #5048",
    ]
    return "\n".join(parts)


# --- 取得 -------------------------------------------------------------------

def http_fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
               user_agent: str = DEFAULT_USER_AGENT) -> str:
    """sitemap を 1 本取得する。非 200 / 例外は FetchError に畳む。

    UA を明示するのは、素の User-Agent だと 403 が返るため (2026-08-12 実測)。
    """
    import requests  # 遅延 import: テストは fetch を差し替えるので requests 不要

    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": user_agent})
    except Exception as exc:  # requests の例外階層に依存しない
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    return resp.text


# --- GitHub 側 (check_history_freshness.py と同じ作法) ----------------------

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
         "--comment", "配信が想定内の鮮度に戻ったため自動 close します (#5042)。"])


def title_for(row: Dict[str, Any]) -> str:
    return {
        "stale": "[delivery] サイトの更新が止まっています",
        "unreachable": "[delivery] サイトに到達できません",
        "unknown": "[delivery] sitemap から更新時刻を読めません",
    }.get(row["status"], "[delivery] 配信に異常があります")


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--sitemap", default=DEFAULT_SITEMAP)
    p.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--now", default=None,
                   help="判定基準時刻 (ISO8601, 既定: UTC 現在)。replay 検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="issue を触らず判定結果と body だけ出す")
    args = p.parse_args(argv)

    now = (dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))

    row = check(args.sitemap, now,
                fetch=lambda u: http_fetch(u, timeout=args.timeout),
                max_age_hours=args.max_age_hours)
    logger.info("status=%s latest=%s age=%s max=%dh %s",
                row["status"], row["latest"],
                "-" if row["age_hours"] is None else f"{row['age_hours']}h",
                row["max_age_hours"], row.get("detail", ""))

    healthy = row["status"] == "ok"
    body = render_body(row, now)

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

    title = title_for(row)
    if existing:
        logger.info("updated %s", update_issue(args.repo, existing, title, body))
    else:
        logger.info("created %s", create_issue(args.repo, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
