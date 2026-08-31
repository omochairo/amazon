"""オリジン障害時に navi.omcha.jp の CNAME を待機系へ倒す (#6205 B-1)。

配信は 2 系統ある。

    本番   navi.omcha.jp CNAME <uuid>.cfargotunnel.com  → cloudflared → NAS nginx
    待機系 navi.omcha.jp CNAME navi-92dc61.gitlab.io    → GitLab Pages

**待機系は劣化した代替ではない。** `pages` ジョブが push ごとに走るので
本番とバイト同一・同 sha で、301 リダイレクト 512 本も 404 も同じに返る
(2026-08-31 実測)。したがって倒すこと自体のコストはほぼゼロで、
**戻すことも急がない**。自動で倒し、戻すのは人手 (`--to nas`) という
非対称な設計にしているのはこのため。

## 何を「オリジン障害」と呼ぶか

**Cloudflare まで届いているのにオリジンが応えない**状態だけを指す。
判定は `https://navi.omcha.jp/build.json` の応答で行う
(このパスは Cache Rule `navi-build-json-nostore` で cache bypass なので
エッジのキャッシュにも Always Online にも化けない)。

| 観測 | 解釈 | 動作 |
|---|---|---|
| 200 + 読める JSON | 健全 | 何もしない |
| `cf-ray` あり + 5xx (520-530 / 502) | **オリジン障害** | 倒す |
| `cf-ray` あり + その他の非 200 | 判定保留 | 鳴らすだけ |
| 接続不能 / DNS 不能 / `cf-ray` 無し | **CF 側か自分側**の問題 | 鳴らすだけ・倒さない |

最後の行が要点で、**CNAME を書き換えても直らない障害で DNS を殴らない**。
Cloudflare 自体が落ちているとき待機系に倒しても待機系も CF の裏にいるので
何も改善せず、レコードの履歴を汚すだけになる。`cf-ray` の有無が
「CF はこちらに届いている」の直接の証拠なので、これを弁別子に使う。

## 誤検知への備え

- **1 回の run の中で連続して失敗したときだけ倒す** (`--attempts` 既定 3・
  `--probe-interval` 既定 20 秒)。状態を持たずに連続性を担保できるので
  commit-back も外部ストアも要らない (`check_delivery_freshness.py` と同じ思想)
- **倒す前に待機系の健全性と鮮度を確認する**。待機系が死んでいる・古いときに
  倒すと部分障害が全面障害に変わる。確認できなければ `blocked` で鳴らすだけ
- **クールダウン**。レコードの `modified_on` が `--cooldown-hours` (既定 2) 以内なら
  倒さない。フラップの抑止で、これも DNS レコード自身を状態として使うので
  こちら側に状態が要らない
- **kill switch**。`FAILOVER_ENABLED` が真でない限り書き換えない (判定と起票だけ)。
  投入直後はこれを外したまま数日回して較正する

## 戻しかた (手動)

    python scripts/failover_dns.py --to nas

戻し先は `--target` か環境変数 `NAS_ORIGIN_CNAME` (リポジトリ変数) から取る。

**トンネルのホスト名を issue にもログにも出さない。** `navi.omcha.jp` は
proxied なので、外から DNS を引いても Cloudflare の IP しか返らず
`<uuid>.cfargotunnel.com` は見えない。このリポジトリは public なので、
issue 本文や Actions のログに出した時点でそれが恒久的に公開される。
認証情報ではない (接続には credentials が要る) が、#6205 が
「LAN の実 IP・Cloudflare のゾーン ID は public に書かない」としているのと
同じ種類のものなので同じ扱いにする。

したがって人が読む出力に載せるのは **向き先の別 (本番 / 待機系) だけ**で、
値そのものは載せない。

## 権限

`CF_DNS_TOKEN` (omcha.jp ゾーン限定 / Zone:Read + DNS:Edit)。
`41-pages-cert-renew.yml` が使っているものと同じで、追加発行は不要。
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
logger = logging.getLogger("failover_dns")

MARKER = "origin-failover"
LABELS = "todo"

DEFAULT_ZONE_NAME = "omcha.jp"
DEFAULT_RECORD_NAME = "navi.omcha.jp"
DEFAULT_ORIGIN_PROBE = "https://navi.omcha.jp/build.json"
DEFAULT_STANDBY_PROBE = "https://navi-92dc61.gitlab.io/build.json"
DEFAULT_STANDBY_CNAME = "navi-92dc61.gitlab.io"

DEFAULT_ATTEMPTS = 3
DEFAULT_PROBE_INTERVAL = 20.0
DEFAULT_COOLDOWN_HOURS = 2.0
# 待機系がこれより古ければ倒さない。
#
# **較正の根拠**: このゲートの目的は「GitLab 側も一緒に死んでいる」を弾くこと
# だけで、鮮度を要求することではない。`pages` は push ごとに走るが、main への
# commit 間隔は実測で最大 6.2h (直近 7 日 392 commit / #6272 の較正) あるので、
# 6h のようなしきい値は**分布のど真ん中**に来る。静かな時間帯に障害が起きた
# だけで `blocked` になり、倒せるはずの障害を倒さなくなる。
#
# 24h にするのは #6205 の設計判断と同じ根拠 —— 待機系が最大 24h 古くて
# 1 日分の記事が欠けることは、配信が止まることよりはるかに軽い。
DEFAULT_STANDBY_MAX_AGE_HOURS = 24.0

DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "navi-origin-failover/1.0 (+https://navi.omcha.jp)"

# Cloudflare がオリジン側の失敗として返すコード。
# 502 = Bad gateway / 520-527 = origin 系 / 530 (= 1033 tunnel not found) を含む。
CF_ORIGIN_ERROR_CODES = frozenset({502, 520, 521, 522, 523, 524, 525, 526, 527, 530})

TUNNEL_SUFFIX = ".cfargotunnel.com"

CF_API = "https://api.cloudflare.com/client/v4"


class ProbeResult(dict):
    """1 回の HTTP プローブの観測。dict のまま持つ (テストで組み立てやすい)。"""


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
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# --- 観測の解釈 -------------------------------------------------------------

def classify_probe(probe: Dict[str, Any]) -> str:
    """1 回の観測を healthy / origin_down / ambiguous に畳む。

    ``probe`` は ``{"status": int|None, "cf_ray": str|None, "body": str|None,
    "error": str|None}``。

    **`cf_ray` の有無が弁別子。** 5xx が返っていても CF の応答でないなら、
    それは CF より手前 (自分の回線 / DNS / CF 自体) の問題であって、
    CNAME を書き換えても直らない。倒す判断には使わない。
    """
    if probe.get("error"):
        return "ambiguous"
    status = probe.get("status")
    if status == 200:
        # 200 でも中身が壊れていれば健全とは言わない。ただし「オリジン障害」でも
        # ないので倒さない (それは check_delivery_freshness.py の unknown の領分)。
        body = probe.get("body")
        try:
            data = json.loads(body) if isinstance(body, str) else None
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("sha"):
            return "healthy"
        return "ambiguous"
    if not probe.get("cf_ray"):
        return "ambiguous"
    if status in CF_ORIGIN_ERROR_CODES:
        return "origin_down"
    return "ambiguous"


def classify_standby(probe: Dict[str, Any], now: dt.datetime,
                     max_age_hours: float = DEFAULT_STANDBY_MAX_AGE_HOURS
                     ) -> Dict[str, Any]:
    """待機系が「倒してよい状態」かを返す。

    ``{"ready": bool, "reason": str, "sha": str|None, "age_hours": float|None}``。
    """
    out: Dict[str, Any] = {"ready": False, "reason": "", "sha": None, "age_hours": None}
    if probe.get("error"):
        out["reason"] = "待機系に到達できない: {}".format(probe["error"])
        return out
    if probe.get("status") != 200:
        out["reason"] = "待機系が HTTP {} を返す".format(probe.get("status"))
        return out
    try:
        data = json.loads(probe.get("body") or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        out["reason"] = "待機系の build.json が読めない"
        return out
    out["sha"] = data.get("sha")
    built_at = parse_iso(data.get("built_at"))
    if built_at is None:
        out["reason"] = "待機系の build.json に読める built_at が無い"
        return out
    age = (now - built_at).total_seconds() / 3600.0
    out["age_hours"] = round(age, 2)
    if age > max_age_hours:
        out["reason"] = "待機系が {:.1f}h 古い (上限 {:.1f}h)".format(age, max_age_hours)
        return out
    out["ready"] = True
    out["reason"] = "待機系は健全 (sha {} / {:.2f}h)".format(
        (out["sha"] or "-")[:10], age)
    return out


def side_of(content: str, standby_cname: str = DEFAULT_STANDBY_CNAME) -> str:
    """CNAME の向き先を nas / standby / unknown に畳む。"""
    text = (content or "").strip().rstrip(".").lower()
    if text.endswith(TUNNEL_SUFFIX):
        return "nas"
    if text == standby_cname.strip().rstrip(".").lower():
        return "standby"
    return "unknown"


def describe_side(content: str, standby_cname: str = DEFAULT_STANDBY_CNAME) -> str:
    """人が読む出力用の向き先ラベル。**値そのものは返さない。**

    このリポジトリは public で、issue 本文も Actions のログも全世界に残る。
    `<uuid>.cfargotunnel.com` は proxied な DNS からは見えないので、
    ここに出すことがそのまま公開になる (#6205 の「Cloudflare のゾーン ID は
    public に書かない」と同じ扱い)。運用で必要なのは「どちらを向いているか」
    だけで、値は要らない。
    """
    side = side_of(content, standby_cname)
    if side == "nas":
        return "本番 (cloudflared tunnel)"
    if side == "standby":
        return "待機系 ({})".format(standby_cname)
    return "**想定外の値**"


def decide(probes: List[Dict[str, Any]], standby: Dict[str, Any],
           record: Optional[Dict[str, Any]], now: dt.datetime, *,
           enabled: bool,
           standby_cname: str = DEFAULT_STANDBY_CNAME,
           cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
           standby_max_age_hours: float = DEFAULT_STANDBY_MAX_AGE_HOURS,
           ) -> Dict[str, Any]:
    """倒すかどうかを決める純関数。HTTP も API も踏まない。

    返す ``action`` は none / failover / blocked / ambiguous / already /
    throttled / disabled / no_record。
    """
    row: Dict[str, Any] = {
        "action": "none", "detail": "", "now": now.isoformat(),
        "probe_verdicts": [classify_probe(p) for p in probes],
        "probes": probes,
        # **CNAME の値そのものは row に載せない** (public な issue / ログに
        # 流れる)。判定に要るのは向き先の別だけ。
        "current_side": None, "current_label": None,
        "standby": None, "target": standby_cname,
        "enabled": bool(enabled),
    }

    if record is None:
        row["action"] = "no_record"
        row["detail"] = "対象の CNAME レコードが見つからない"
        return row

    content = record.get("content") or ""
    row["current_side"] = side_of(content, standby_cname)
    row["current_label"] = describe_side(content, standby_cname)

    verdicts = row["probe_verdicts"]
    if not verdicts:
        row["action"] = "ambiguous"
        row["detail"] = "プローブが 1 回も走っていない"
        return row

    # **1 回でも健全なら健全。** 連続失敗を要求するのと同じことを裏から言っている。
    if "healthy" in verdicts:
        row["action"] = "none"
        row["detail"] = "オリジンは応答している"
        return row

    if not all(v == "origin_down" for v in verdicts):
        # cf-ray が無い失敗が混ざる = CF より手前の問題を否定できない。
        row["action"] = "ambiguous"
        row["detail"] = (
            "オリジン障害と断定できない観測が混ざっている ({})。"
            "CNAME を書き換えても直らない障害で DNS を殴らない".format(
                "/".join(verdicts))
        )
        return row

    # ここから先は「CF は届いていて、オリジンだけが応えていない」が確定した状態。
    if row["current_side"] == "standby":
        row["action"] = "already"
        row["detail"] = "すでに待機系を向いている (戻すのは手動: --to nas)"
        return row
    if row["current_side"] == "unknown":
        row["action"] = "blocked"
        row["detail"] = (
            "現在の CNAME が本番でも待機系でもない値を指している。"
            "自動で書き換えない (値は載せない — public repo のため)"
        )
        return row

    standby_state = classify_standby(standby, now, max_age_hours=standby_max_age_hours)
    row["standby"] = standby_state
    if not standby_state["ready"]:
        row["action"] = "blocked"
        row["detail"] = (
            "オリジンは落ちているが待機系に倒せない — {}。"
            "倒すと部分障害が全面障害になる".format(standby_state["reason"])
        )
        return row

    modified = parse_iso(record.get("modified_on"))
    if modified is not None:
        since = (now - modified).total_seconds() / 3600.0
        if since < cooldown_hours:
            row["action"] = "throttled"
            row["detail"] = (
                "レコードを {:.2f}h 前に書き換えたばかり (クールダウン {:.1f}h)。"
                "フラップ抑止のため倒さない".format(since, cooldown_hours)
            )
            return row

    if not enabled:
        row["action"] = "disabled"
        row["detail"] = (
            "倒す条件は満たしているが FAILOVER_ENABLED が真でないため"
            "書き換えない (dry-run 期間)"
        )
        return row

    row["action"] = "failover"
    row["detail"] = "オリジン障害を確認。待機系 {} へ倒す".format(standby_cname)
    return row


# --- HTTP -------------------------------------------------------------------

def http_probe(url: str, timeout: float = DEFAULT_TIMEOUT,
               user_agent: str = DEFAULT_USER_AGENT,
               cache_bust: bool = True) -> Dict[str, Any]:
    """1 回叩いて観測を返す。例外は握って ``error`` に畳む (判定は decide 側)。"""
    import requests

    probe_url = url
    if cache_bust:
        sep = "&" if "?" in url else "?"
        probe_url = "{}{}t={}".format(url, sep, int(time.time()))
    try:
        resp = requests.get(probe_url, timeout=timeout, allow_redirects=True,
                            headers={"User-Agent": user_agent,
                                     "Cache-Control": "no-cache"})
    except Exception as exc:  # requests の例外階層に依存しない
        return {"url": probe_url, "status": None, "cf_ray": None, "body": None,
                "error": "{}: {}".format(type(exc).__name__, exc)}
    # 本文は判定に使う分だけ。CF のエラーページは数十 KB あるので丸ごと持たない。
    return {"url": probe_url, "status": resp.status_code,
            "cf_ray": resp.headers.get("cf-ray"),
            "body": resp.text[:4096], "error": None}


def probe_origin(url: str, attempts: int, interval: float, timeout: float,
                 sleep: Callable[[float], None] = time.sleep,
                 probe: Optional[Callable[[str], Dict[str, Any]]] = None
                 ) -> List[Dict[str, Any]]:
    """健全なら 1 回で切り上げ、失敗したときだけ間隔を空けて追試する。

    正常時 (圧倒的多数) のコストを 1 リクエストに保ちつつ、異常時だけ
    「連続して失敗した」を 1 run の中で確かめる。
    """
    fn = probe or (lambda u: http_probe(u, timeout=timeout))
    out: List[Dict[str, Any]] = []
    for i in range(max(1, attempts)):
        if i:
            sleep(interval)
        result = fn(url)
        out.append(result)
        if classify_probe(result) == "healthy":
            break
    return out


# --- Cloudflare -------------------------------------------------------------

def _cf(token: str, method: str, path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    import requests

    resp = requests.request(
        method, "{}{}".format(CF_API, path), timeout=timeout,
        headers={"Authorization": "Bearer {}".format(token),
                 "Content-Type": "application/json"},
        data=json.dumps(payload) if payload is not None else None)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError("Cloudflare API が JSON を返さない: HTTP {} {}".format(
            resp.status_code, exc)) from exc
    if not data.get("success"):
        raise RuntimeError("Cloudflare API 失敗 ({} {}): {}".format(
            method, path, json.dumps(data.get("errors"), ensure_ascii=False)[:400]))
    return data


def get_zone_id(token: str, zone_name: str) -> str:
    data = _cf(token, "GET", "/zones?name={}".format(zone_name))
    result = data.get("result") or []
    if not result:
        raise RuntimeError("ゾーン {} が見つからない".format(zone_name))
    return result[0]["id"]


def get_record(token: str, zone_id: str, name: str) -> Optional[Dict[str, Any]]:
    data = _cf(token, "GET", "/zones/{}/dns_records?type=CNAME&name={}".format(
        zone_id, name))
    result = data.get("result") or []
    return result[0] if result else None


def patch_record(token: str, zone_id: str, record: Dict[str, Any],
                 content: str) -> Dict[str, Any]:
    """CNAME の向き先だけ差し替える。

    ``proxied`` は現在値を明示して送る。**省くと API 既定 (false) に落ちて
    プロキシが外れ、エッジ証明書ごと配信が壊れる。** 現在値を運ぶのは
    「触っていないものを変えない」ためで、真偽を推測しない。
    """
    payload = {"type": "CNAME", "name": record["name"], "content": content,
               "proxied": bool(record.get("proxied")), "ttl": record.get("ttl", 1)}
    data = _cf(token, "PATCH", "/zones/{}/dns_records/{}".format(
        zone_id, record["id"]), payload)
    return data.get("result") or {}


# --- GitHub (check_delivery_freshness.py と同じ作法) -------------------------

def _gh(args: List[str]) -> str:
    import subprocess
    res = subprocess.run(["gh", *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return res.stdout


def get_open_issue(repo: str) -> Optional[Dict[str, Any]]:
    query = 'repo:{} is:issue is:open in:body "{}"'.format(repo, MARKER)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=10"])
    items = json.loads(out).get("items", [])
    if not items:
        return None
    return {"number": items[0]["number"], "body": items[0].get("body") or ""}


def create_issue(repo: str, title: str, body: str) -> str:
    return _gh(["issue", "create", "-R", repo, "--title", title,
                "--label", LABELS, "--body", body]).strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    return _gh(["issue", "edit", str(number), "-R", repo,
                "--title", title, "--body", body]).strip()


def close_issue(repo: str, number: int, comment: str) -> None:
    _gh(["issue", "close", str(number), "-R", repo, "--comment", comment])


# --- 起票 -------------------------------------------------------------------

TITLES = {
    "failover": "[delivery][failover] オリジン障害のため待機系へ倒しました",
    "disabled": "[delivery][failover] オリジン障害を検出しました (自動切替は未武装)",
    "blocked": "[delivery][failover] オリジン障害。待機系に倒せません",
    "throttled": "[delivery][failover] オリジン障害。クールダウン中で倒しません",
    "already": "[delivery][failover] 待機系で配信中です (戻しは手動)",
    "ambiguous": "[delivery][failover] 配信に到達できません (オリジン障害と断定できず)",
    "no_record": "[delivery][failover] 対象の CNAME レコードが見つかりません",
}

# issue を閉じる (= 起票しない) 判定。`failback` は「人が戻し終えた」なので
# 倒したときの issue はここで役目を終える。
HEALTHY_ACTIONS = frozenset({"none", "failback"})


def title_for(row: Dict[str, Any]) -> str:
    return TITLES.get(row["action"], "[delivery][failover] 配信に異常があります")


def render_body(row: Dict[str, Any]) -> str:
    standby = row.get("standby") or {}
    parts: List[str] = [
        "<!-- {} -->".format(MARKER),
    ]
    parts += [
        "",
        "**{}** ({} 時点)。".format(title_for(row).split("] ", 1)[-1], row["now"]),
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        "| 判定 | `{}` |".format(row["action"]),
        "| 向き先 (判定時) | {} |".format(row.get("current_label") or "-"),
        "| 自動切替 | {} |".format("武装" if row.get("enabled") else "**未武装**"),
        "| プローブ | {} |".format(" / ".join(row.get("probe_verdicts") or []) or "-"),
        "| 待機系 | {} |".format(standby.get("reason") or "未確認"),
    ]
    if row.get("new_label"):
        parts.append("| 書き換え後 | {} |".format(row["new_label"]))
    parts += [
        "| 詳細 | {} |".format(row.get("detail") or "-"),
        "",
        "## 観測",
        "",
        "| # | URL | HTTP | cf-ray | 解釈 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, (probe, verdict) in enumerate(
            zip(row.get("probes") or [], row.get("probe_verdicts") or []), 1):
        parts.append("| {} | {} | {} | {} | `{}` |".format(
            i, probe.get("url") or "-",
            probe.get("status") if probe.get("status") is not None
            else "({})".format(probe.get("error") or "-"),
            "あり" if probe.get("cf_ray") else "なし", verdict))
    parts += [
        "",
        "## 見かた",
        "",
        "- `failover` — 待機系に倒しました。**配信は継続しています**"
        "(待機系は push ごとに更新され本番とバイト同一)。"
        "NAS を復旧したうえで、戻すのは手動です",
        "- `disabled` — 倒す条件は満たしましたが `FAILOVER_ENABLED` が"
        "真でないため書き換えていません。**配信は落ちたままです**",
        "- `blocked` — 待機系が健全でない / CNAME の向き先が想定外。"
        "倒すと部分障害が全面障害になるので止めました。手当てが要ります",
        "- `ambiguous` — `cf-ray` が無い失敗が混ざっています。"
        "CF 自体か手元の回線の問題である可能性を否定できないため、"
        "**CNAME は触りません**。GitHub Actions runner 側の一時障害でも出ます",
        "- `already` — すでに待機系で配信中です",
        "",
        "## 戻しかた",
        "",
        "```",
        "gh workflow run 53-origin-failover.yml -R <repo> -f direction=nas",
        "```",
        "",
        "戻し先はリポジトリ変数 `NAS_ORIGIN_CNAME` から取ります"
        "(未設定なら `-f target=...` を明示)。"
        "**この issue にトンネルのホスト名は載せていません** —— "
        "public repo なので、載せた時点で恒久的に公開されるためです。",
        "",
        "**戻す前に NAS 側 (cloudflared / nginx / navi-switch) の復旧を確認すること。**",
        "",
        "- マーカー `<!-- {} -->` で同一 open Issue を特定し body を更新します".format(MARKER),
        "- オリジンが応答に戻り、かつ本番を向いていれば自動 close します",
        "",
        "Refs #6205, amazon-home-ops#70",
    ]
    return "\n".join(parts)


# --- 実行 -------------------------------------------------------------------

def _env_true(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def run_failback(args: argparse.Namespace, token: str, now: dt.datetime) -> Dict[str, Any]:
    """待機系 → 本番 (NAS) へ戻す。人が明示的に起こしたときだけ走る。"""
    zone_id = args.zone_id or get_zone_id(token, args.zone_name)
    record = get_record(token, zone_id, args.record_name)
    row: Dict[str, Any] = {
        "action": "failback", "now": now.isoformat(), "enabled": True,
        "probes": [], "probe_verdicts": [], "standby": None,
        "current_side": side_of((record or {}).get("content") or "", args.standby_cname),
        "current_label": describe_side((record or {}).get("content") or "",
                                       args.standby_cname),
        "detail": "",
    }
    if record is None:
        row["action"] = "no_record"
        row["detail"] = "対象の CNAME レコードが見つからない"
        return row

    # 戻し先はリポジトリ変数から取る。**issue 本文には置かない** ——
    # public repo なので、置いた時点でトンネルのホスト名が恒久的に公開される。
    target = args.target or (os.environ.get("NAS_ORIGIN_CNAME") or "").strip()
    if not target:
        row["action"] = "blocked"
        row["detail"] = (
            "戻し先が分からない。リポジトリ変数 `NAS_ORIGIN_CNAME` を設定するか、"
            "`--target` / `-f target=...` を渡すこと。"
            "値は private の amazon-home-ops (docker/navi) にある"
        )
        return row
    if side_of(target, args.standby_cname) != "nas":
        row["action"] = "blocked"
        row["detail"] = "戻し先が cfargotunnel.com ではない (値は載せない)"
        return row
    if row["current_side"] == "nas":
        row["action"] = "none"
        row["detail"] = "すでに本番 (NAS) を向いている"
        return row

    if args.dry_run:
        row["detail"] = "[dry-run] {} → 本番 (cloudflared tunnel) に戻す".format(
            row["current_label"])
        return row
    result = patch_record(token, zone_id, record, target)
    row["new_label"] = describe_side(result.get("content", target), args.standby_cname)
    row["detail"] = "{} → {} に戻した".format(row["current_label"], row["new_label"])
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--to", choices=("standby", "nas"), default="standby",
                   help="standby=障害を判定して倒す (既定) / nas=手動で戻す")
    p.add_argument("--target", default=None,
                   help="--to nas の戻し先 CNAME。省くと issue から取り出す")
    p.add_argument("--zone-name", default=DEFAULT_ZONE_NAME)
    p.add_argument("--zone-id", default=os.environ.get("CF_ZONE_ID"))
    p.add_argument("--record-name", default=DEFAULT_RECORD_NAME)
    p.add_argument("--origin-probe", default=DEFAULT_ORIGIN_PROBE)
    p.add_argument("--standby-probe", default=DEFAULT_STANDBY_PROBE)
    p.add_argument("--standby-cname", default=DEFAULT_STANDBY_CNAME)
    p.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    p.add_argument("--probe-interval", type=float, default=DEFAULT_PROBE_INTERVAL)
    p.add_argument("--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_HOURS)
    p.add_argument("--standby-max-age-hours", type=float,
                   default=DEFAULT_STANDBY_MAX_AGE_HOURS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--now", default=None, help="判定基準時刻 (ISO8601)。replay 検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="DNS も issue も触らず、判定と本文だけ出す")
    args = p.parse_args(argv)

    now = (dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))

    token = os.environ.get("CF_DNS_TOKEN") or ""
    if not token:
        logger.error("CF_DNS_TOKEN が要ります")
        return 2

    if args.to == "nas":
        row = run_failback(args, token, now)
    else:
        probes = probe_origin(args.origin_probe, args.attempts,
                              args.probe_interval, args.timeout)
        # 待機系は倒す可能性があるときだけ叩く (正常時に GitLab を突かない)。
        needs_standby = all(classify_probe(x) == "origin_down" for x in probes)
        standby = (http_probe(args.standby_probe, timeout=args.timeout,
                              cache_bust=False)
                   if needs_standby else {"error": "未確認 (オリジンは応答している)"})

        zone_id = args.zone_id or get_zone_id(token, args.zone_name)
        record = get_record(token, zone_id, args.record_name)
        row = decide(probes, standby, record, now,
                     enabled=_env_true("FAILOVER_ENABLED"),
                     standby_cname=args.standby_cname,
                     cooldown_hours=args.cooldown_hours,
                     standby_max_age_hours=args.standby_max_age_hours)

        if row["action"] == "failover" and not args.dry_run:
            result = patch_record(token, zone_id, record, args.standby_cname)
            row["new_label"] = describe_side(
                result.get("content", args.standby_cname), args.standby_cname)
            logger.warning("向き先を %s → %s に書き換えた",
                           row["current_label"], row["new_label"])
        elif row["action"] == "failover":
            row["detail"] += " [dry-run のため書き換えていない]"

    # **CNAME の値をログに出さない。** public repo の Actions ログは
    # 全世界に残り、proxied な DNS からは見えないホスト名がここで公開される。
    logger.info("action=%s side=%s %s",
                row["action"], row.get("current_side"), row.get("detail"))

    healthy = row["action"] in HEALTHY_ACTIONS
    body = render_body(row)

    if args.dry_run:
        print(body if not healthy else "healthy — 起票しません\n{}".format(row["detail"]))
        return 0
    if not args.repo:
        logger.error("--repo (または環境変数 REPO) が要ります")
        return 2

    existing = get_open_issue(args.repo)
    if healthy:
        if existing:
            close_issue(args.repo, existing["number"],
                        "{} (#6205 B-1)。".format(
                            row.get("detail")
                            or "オリジンが応答に戻り、本番 (NAS) を向いています"))
            logger.info("closed #%d", existing["number"])
        else:
            logger.info("healthy — 何もしません")
        return 0

    title = title_for(row)
    if existing:
        logger.info("updated %s", update_issue(args.repo, existing["number"], title, body))
    else:
        logger.info("created %s", create_issue(args.repo, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
