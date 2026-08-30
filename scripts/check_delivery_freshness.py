"""公開サイトが更新され続けているかを外側から 1 本で見る (#5042 / #6205 T9)。

判定材料は ``https://navi.omcha.jp/build.json``。配信ビルドが毎回焼く
``{"sha", "built_at", "ref"}`` で、**いまサイトに乗っているのはどのコミットで、
それはいつ焼かれたか**を外から直接読める。

なぜ sitemap の lastmod をやめたか (2026-08-30):
  2026-08-28 に GitLab Pages のサイズ上限で **本番が約 19 時間凍った**とき、
  この監視は鳴らなかった。sitemap の lastmod は「その URL を最後に動かした
  コミットの時刻」であって「配信が走った時刻」ではない。配信が止まっても
  古い sitemap が古い lastmod を返し続けるだけで、閾値 36h には届かない。
  **止まったことを検出するには、配信そのものが刻む時刻を見るしかない。**

見る異常は 2 種類:

1. ``stale`` —— ``built_at`` が古い。原因が何であれ配信が止まっている
2. ``behind`` —— ``built_at`` は新しいのに ``sha`` が main の HEAD でなく、
   その HEAD 自体が閾値より古い。「時計は進んでいるのに中身が追いついていない」
   経路 (mirror が壊れて GitLab 側の main が進んでいない等) を捕まえる

なぜ必要か:
  配信は GitHub と GitLab / NAS をまたいで成立している。

      main へ push → 40-mirror-to-gitlab.yml が git push
                   → GitLab CI の pages / deploy-nas → navi.omcha.jp

  GitHub 側の責務は ``git push`` が成功するところで終わっており、その先の
  結果を見に行くコードはリポジトリ内に無い。したがって **配信が落ちても
  GitHub の run は全部緑のまま、Issue も立たず、記事だけが増え続けて
  サイトが凍る**。#4789 が張った網の 4 本目にあたる。

設計判断:
- **原因非依存**。GitLab のパイプライン API を叩いて成否を見る作りにしない。
  それだと「CI は成功したが配信側で止まった」を取りこぼすし、監視対象の CI に
  監視が依存する。外から見えた事実だけで判定する。
- **監視は配信と別のインフラに置く**。この script は GitHub Actions 側で走らせる。
- **エッジを迂回する**。``?t=<epoch>`` を付けて取得する。エッジのキャッシュを
  読むと「配信は止まっているのに新しく見える」ことがある。
- **commit-back しない**。判定は毎回その場で取り直せるので状態を持たない。
- **判定不能を ok に潰さない**。取得失敗も壊れた JSON も、それ自体が異常。

起票規律 (CLAUDE.md: GitHub API バースト禁止):
  マーカー ``<!-- delivery-freshness-monitor -->`` で単一 issue を特定して body を
  更新し、健全に戻ったら close する。1 run あたりの書き込みは最大 1 回。

較正:
  配信は main への push ごとに走り、push は 1 日 50 本前後ある。2026-08-30 の
  実測で build から反映までは数分。既定閾値 3h は **入れた日に鳴らない** =
  鳴ったら必ず異常。閾値は DEFAULT_MAX_AGE_HOURS 一箇所に置く。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_delivery_freshness")

MARKER = "delivery-freshness-monitor"
LABELS = "tech-debt,todo"
DEFAULT_BUILD_JSON = "https://navi.omcha.jp/build.json"
DEFAULT_MAX_AGE_HOURS = 3
DEFAULT_USER_AGENT = "navi-delivery-freshness/2.0 (+https://navi.omcha.jp)"
DEFAULT_TIMEOUT = 30.0


class FetchError(Exception):
    """build.json を取得できなかった (DNS / 接続 / 非 200 / タイムアウト)。"""


def parse_iso(value: Any) -> Optional[dt.datetime]:
    """ISO8601 を aware な UTC datetime にする。読めなければ None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # tz 無しは UTC とみなす。ここで捨てると、tz を落とす実装に変わった
        # 瞬間に「読めない」で誤報する。
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def check(url: str, now: dt.datetime, fetch: Callable[[str], str],
          max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
          head: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """配信の状態を 1 件返す。status は ok / stale / behind / unreachable / unknown。

    ``head`` は main の HEAD 情報 ``{"sha", "committed_at"}``。None なら
    sha の突合をしない (取得できなかった場合も鮮度判定だけは続ける)。
    """
    row: Dict[str, Any] = {
        "status": "unknown", "url": url, "built_at": None, "sha": None,
        "age_hours": None, "max_age_hours": max_age_hours,
        "head_sha": (head or {}).get("sha"), "detail": "",
    }
    try:
        raw = fetch(url)
    except FetchError as exc:
        # 取得できないこと自体が最重度の異常 (サイトが落ちている / DNS /
        # 証明書失効)。ここを ok に潰さない。
        row["status"] = "unreachable"
        row["detail"] = str(exc)
        return row

    try:
        data = json.loads(raw)
    except ValueError as exc:
        row["detail"] = "build.json が JSON として読めない: {}".format(exc)
        return row
    if not isinstance(data, dict):
        row["detail"] = "build.json が object ではない"
        return row

    built_at = parse_iso(data.get("built_at"))
    sha = data.get("sha")
    row["sha"] = sha if isinstance(sha, str) else None
    if built_at is None:
        row["detail"] = "build.json に読める built_at が無い"
        return row

    row["built_at"] = built_at.isoformat()
    age_hours = (now - built_at).total_seconds() / 3600.0
    row["age_hours"] = round(age_hours, 1)

    if age_hours > max_age_hours:
        row["status"] = "stale"
        return row

    # built_at は新しい。次に「中身が追いついているか」を見る。
    # HEAD が動いた直後は sha が違って当たり前なので、**HEAD 自体が閾値より
    # 古いのにまだ配信されていない** ときだけ異常とする。
    head_sha = (head or {}).get("sha")
    if head_sha and row["sha"] and head_sha != row["sha"]:
        head_at = parse_iso((head or {}).get("committed_at"))
        if head_at is not None and (now - head_at).total_seconds() / 3600.0 > max_age_hours:
            row["status"] = "behind"
            row["detail"] = (
                "配信中の sha は {} だが、main の HEAD {} は {} で閾値より古い".format(
                    row["sha"][:10], head_sha[:10], head_at.isoformat())
            )
            return row

    row["status"] = "ok"
    return row


def render_body(row: Dict[str, Any], now: dt.datetime) -> str:
    age = "-" if row["age_hours"] is None else "{}h".format(row["age_hours"])
    headline = {
        "stale": "配信が止まっています",
        "behind": "配信は動いているのに中身が古いままです",
        "unreachable": "サイトに到達できません",
        "unknown": "build.json から配信時刻を読めません",
    }.get(row["status"], "配信に異常があります")
    parts: List[str] = [
        "<!-- {} -->".format(MARKER),
        "",
        "**{}** ({} 時点)。".format(headline, now.isoformat()),
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        "| 状態 | `{}` |".format(row["status"]),
        "| build.json | {} |".format(row["url"]),
        "| built_at | {} |".format(row["built_at"] or "-"),
        "| 配信中の sha | `{}` |".format(row["sha"] or "-"),
        "| main の HEAD | `{}` |".format(row["head_sha"] or "-"),
        "| 経過 | {} |".format(age),
        "| 上限 | {}h |".format(row["max_age_hours"]),
    ]
    if row.get("detail"):
        parts.append("| 詳細 | {} |".format(row["detail"]))
    parts += [
        "",
        "## なぜこれが GitHub 側で緑のまま起きるか",
        "",
        "配信は複数の CI をまたいでいます。",
        "",
        "```",
        "main へ push → 40-mirror-to-gitlab.yml が git push",
        "             → GitLab CI の pages / deploy-nas → navi.omcha.jp",
        "```",
        "",
        "GitHub 側の責務は `git push` が成功するところで終わりです。"
        "その先が落ちても GitHub の run は全部緑のままなので、"
        "**記事だけが増え続けてサイトが凍ります**。"
        "2026-08-28 の 19 時間停止が実例です (#6205)。",
        "",
        "## 見かた",
        "",
        "- `stale` — 配信が止まっている。GitLab 側のパイプライン "
        "(`pages` / `deploy-nas`) と、`40-mirror-to-gitlab.yml` の直近 run "
        "(非 fast-forward で reject されていないか) を見ること",
        "- `behind` — 配信は走っているのに古いコミットのまま。mirror が壊れて "
        "GitLab 側の main が進んでいない可能性",
        "- `unreachable` — 取得できない。サイト自体の停止 / DNS / 証明書を疑う "
        "(証明書は `41-pages-cert-renew.yml`)",
        "- `unknown` — 200 は返るが `build.json` が読めない。ビルドは通ったが"
        "生成物が壊れている可能性",
        "",
        "- マーカー `<!-- {} -->` で同一 open Issue を特定し、body を毎回更新します".format(MARKER),
        "- 配信が健全に戻ったら自動 close します",
        "",
        "Refs #5042, #4789, #6205",
    ]
    return "\n".join(parts)


# --- 取得 -------------------------------------------------------------------

def http_fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
               user_agent: str = DEFAULT_USER_AGENT) -> str:
    """build.json を 1 本取得する。非 200 / 例外は FetchError に畳む。

    **エッジを迂回するためにキャッシュバスターを付ける。** エッジのキャッシュを
    読むと「配信は止まっているのに新しく見える」ことがある。
    UA を明示するのは、素の User-Agent だと 403 が返るため (2026-08-12 実測)。
    """
    import requests  # 遅延 import: テストは fetch を差し替えるので requests 不要

    sep = "&" if "?" in url else "?"
    probe = "{}{}t={}".format(url, sep, int(time.time()))
    try:
        resp = requests.get(probe, timeout=timeout,
                            headers={"User-Agent": user_agent,
                                     "Cache-Control": "no-cache"})
    except Exception as exc:  # requests の例外階層に依存しない
        raise FetchError("{}: {}".format(type(exc).__name__, exc)) from exc
    if resp.status_code != 200:
        raise FetchError("HTTP {}".format(resp.status_code))
    return resp.text


# --- GitHub 側 (check_history_freshness.py と同じ作法) ----------------------

def _gh(args: List[str]) -> str:
    import subprocess
    # encoding を明示する。既定のロケール依存デコード (Windows では cp932) だと
    # 日本語を含む API 応答で UnicodeDecodeError になり、**stdout が None に
    # なったまま returncode 0 で返ってくる** (2026-08-30 に手元で踏んだ)。
    res = subprocess.run(["gh", *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return res.stdout


def get_head(repo: str, branch: str = "main") -> Optional[Dict[str, Any]]:
    """main の HEAD の sha と commit 時刻。取れなければ None (鮮度判定は続ける)。"""
    try:
        data = json.loads(_gh(["api", "repos/{}/commits/{}".format(repo, branch)]))
        return {"sha": data["sha"],
                "committed_at": data["commit"]["committer"]["date"]}
    except Exception as exc:
        logger.warning("main の HEAD を取れなかった: %s", exc)
        return None


def get_open_issue(repo: str) -> Optional[int]:
    query = 'repo:{} is:issue is:open in:body "{}"'.format(repo, MARKER)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=10"])
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
        "stale": "[delivery] 配信が止まっています",
        "behind": "[delivery] 配信は動いているのに中身が古いままです",
        "unreachable": "[delivery] サイトに到達できません",
        "unknown": "[delivery] build.json から配信時刻を読めません",
    }.get(row["status"], "[delivery] 配信に異常があります")


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--build-json", default=DEFAULT_BUILD_JSON)
    p.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--now", default=None,
                   help="判定基準時刻 (ISO8601, 既定: UTC 現在)。replay 検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="issue を触らず判定結果と body だけ出す")
    args = p.parse_args(argv)

    now = (dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))

    head = get_head(args.repo) if args.repo else None

    row = check(args.build_json, now,
                fetch=lambda u: http_fetch(u, timeout=args.timeout),
                max_age_hours=args.max_age_hours, head=head)
    logger.info("status=%s built_at=%s sha=%s head=%s age=%s max=%dh %s",
                row["status"], row["built_at"], row["sha"], row["head_sha"],
                "-" if row["age_hours"] is None else "{}h".format(row["age_hours"]),
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
