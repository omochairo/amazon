#!/usr/bin/env python3
"""inbox の未対応返信に対して、返信案を Claude (agy 経由) に起草させる。

fetch_sns_replies.py が貯めた inbox を読み、まだ返していないものへ返信案を
2 案ずつ付ける。**送信はしない**。送信は post_sns_reply.py が人手承認を
受けてから行う。

なぜ agy 経由か:
  K8 LLM ワーカー側で `agy` (Antigravity CLI) がファイルベース認証済みで、
  Claude 系モデルを owner の定額クォータで回せる (mine_experience.py の
  gather_antigravity と同じ経路・同じ認証)。新規 API キーを増やさない。
  `agy --model claude-sonnet-4-6 --print=<prompt>` で 1 回 1 プロンプト。

ペルソナは **private リポジトリの jules/ overlay からしか読まない**:
  X・Bluesky = いろママ / Threads = いろパパ の人格定義は amazon-navi-brain 側の
  資産で、
  public なこのリポジトリに複製しない。overlay が無い環境では起草を拒否する
  (適当なペルソナで書くくらいなら書かない方が良い。中の人が違う人格で
  返信するのは、放置よりダメージが大きい)。

使い方:
    python scripts/draft_sns_reply.py --limit 5
    python scripts/draft_sns_reply.py --limit 1 --dry-run   # プロンプトだけ出す
    python scripts/draft_sns_reply.py --redraft             # 既存案を破棄して作り直す

env:
    SNS_INBOX_DIR      inbox の置き場所
    AGY_MODEL          既定 claude-sonnet-4-6
    AGY_TIMEOUT_S      既定 300

exit code:
    0 = 走り切った (対象 0 件も成功)
    1 = agy が使えない / ペルソナが無い等で 1 件も起草できなかった
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import sns_inbox_store as store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
JULES_DIR = REPO_ROOT / "jules"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_S = 300

# channel -> ペルソナ定義ファイル (private overlay)。
#
# bluesky は専用ペルソナが未定義なので X (いろママ) を流用する。**本文の実態が
# いろママだから**であって、表示名で決めているのではない。2026-09-05 に一度
# いろパパへ倒して間違えたので、根拠を残す:
#
#   - notify_engagement.py は `channel == "x"` のときだけ Bluesky にミラーする。
#     つまり日々の人格投稿は X の本文そのもの
#   - 直近 100 投稿の一人称: 私 10 / 僕 0、夫 3 / 妻 0、ママ 5 / パパ 1
#
# @omochairo.bsky.social の displayName は当時「いろパパ＠おもちゃいろ」だったが、
# これは設定の取り違えで、owner が いろママ へ修正すると判断済み。**表示名 1 点で
# 人格を断定したのが誤りだった** — 実際に何が投稿されているかを先に見ること。
#
# ここを間違えると「中の人が急に別人格で返信する」ことになり、放置より悪い。
PERSONA_FILES = {
    "x": "PROMPT_ENGAGEMENT_X_IROMAMA_DAILY.md",
    "bluesky": "PROMPT_ENGAGEMENT_X_IROMAMA_DAILY.md",
    "threads": "PROMPT_ENGAGEMENT_THREADS_IROPAPA_DAILY.md",
}

# ペルソナ定義から持ってくるのは人格の節だけ。投稿本数・queue 形式など
# 生成タスク固有の指示まで渡すと、返信ではなく新規投稿を書き始める。
PERSONA_SECTION_RE = re.compile(
    r"^##\s*1\.\s*あなたの役割.*?(?=^##\s*2\.)", re.MULTILINE | re.DOTALL,
)

VERDICT_RE = re.compile(r"^判定[:：]\s*(.+)$", re.MULTILINE)
DRAFT_RE = re.compile(r"^案\s*([12])[:：]\s*(.+?)(?=^案\s*[12][:：]|\Z)", re.MULTILINE | re.DOTALL)

# 返信の長さ上限。X は weighted 280 だが日本語は 1 文字 2 weight なので実質 140。
CHANNEL_LIMITS = {"x": 130, "threads": 400, "bluesky": 250}


class DraftError(RuntimeError):
    pass


def load_persona(channel: str) -> str:
    filename = PERSONA_FILES.get(channel)
    if not filename:
        raise DraftError(f"channel {channel} のペルソナ定義が未割当")
    path = JULES_DIR / filename
    if not path.exists():
        raise DraftError(
            f"{path} が無い — jules/ overlay (amazon-navi-brain) が checkout されていない。"
            "ペルソナ不明のまま起草はしない",
        )
    text = path.read_text(encoding="utf-8")
    m = PERSONA_SECTION_RE.search(text)
    if not m:
        raise DraftError(f"{path} から「1. あなたの役割」節を切り出せなかった")
    return m.group(0).strip()


def build_prompt(rec: dict, persona: str) -> str:
    limit = CHANNEL_LIMITS.get(rec["channel"], 200)
    return f"""あなたは SNS アカウントの中の人として、届いた返信に返事を書きます。
以下のペルソナ定義に**完全に従って**ください。

--- ペルソナ定義ここから ---
{persona}
--- ペルソナ定義ここまで ---

## 届いた反応

- チャネル: {rec['channel']}
- 種別: {rec['kind']}
- 相手: @{rec.get('author') or '不明'}
- 受信時刻: {rec.get('created_at') or '不明'}

相手の本文:
\"\"\"
{rec['text']}
\"\"\"

## あなたの仕事

1. **そもそも返信すべきか**を判断する。次のいずれかなら「返信しない」:
   - スパム・宣伝・アフィリエイト誘導・明らかな bot
   - 絡み目的の攻撃・侮辱。反応するほど損をする
   - 医療・発達・安全性の断定的な助言を求めており、こちらが答えると害になりうる
   - 内容が薄く、返しても会話にならない (単なる絵文字だけ 等)
2. 返信すると判断したら、返信案を **2 案** 書く。

## 返信案の制約 (違反したら書き直す)

- **{limit} 文字以内**。短い方が良い
- 事実を作らない。商品名・価格・仕様・数値を**推測で書かない**。手元に無い情報は書かない
- 医療・発達・安全性の断定をしない (「〜と言われています」もダメ。触れない)
- 記事 URL や商品リンクを貼らない。宣伝しない
- 末尾に署名を付けない。「いろママです」等と名乗らない
- 自動生成であることを匂わせない
- 相手の言ったことに具体的に応答する。定型の相槌で終わらせない
- 2 案は**方向性を変える**。同じ内容の言い換えにしない

## 出力形式 (この形式ちょうどで出力する。前置き・後書き禁止)

判定: 返信する
理由: <1行>
案1: <本文>
案2: <本文>

返信しない場合は次の形式:

判定: 返信しない
理由: <1行>
"""


def build_agy_argv(prompt: str, model: str) -> list[str]:
    """agy の argv を組む。

    `--print` は**次のトークンを prompt として食う**ので、
    `agy --print --model X "本文"` と書くと `--model` が prompt になり、
    本文は無視されたまま exit 2 になる (2026-09-05 の初回 run で実際に発生。
    agy 自身が "Attach the prompt to the flag (--print='your prompt') and
    move --model elsewhere" と言ってくる)。

    そこで **--model を先に置き、prompt は --print= に添付する**。
    この形は 2026-09-05 に agy 1.1.24 で実測して rc=0 を確認済み。
    """
    return ["agy", "--model", model, f"--print={prompt}"]


def call_agy(prompt: str, model: str, timeout_s: int) -> str:
    """agy をヘッドレス実行して応答テキストを返す。

    mine_experience.gather_antigravity と同じ呼び方 (dbus-run-session 経由)。
    Windows には dbus-run-session が無いので、無ければ agy を直接叩く。
    """
    base = build_agy_argv(prompt, model)
    cmds = [["dbus-run-session", "--", *base], base]

    last_error = ""
    for cmd in cmds:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8",
            )
        except FileNotFoundError:
            last_error = f"{cmd[0]} が PATH に無い"
            continue
        except subprocess.TimeoutExpired:
            raise DraftError(f"agy が timeout ({timeout_s}s)") from None

        if result.returncode != 0:
            raise DraftError(
                f"agy が非ゼロ終了 (code {result.returncode}): {(result.stderr or '')[:200]}",
            )
        text = (result.stdout or "").strip()
        if not text:
            raise DraftError("agy から空応答")
        return text

    raise DraftError(last_error or "agy を起動できなかった")


def parse_response(text: str) -> tuple[bool, str, list[str]]:
    """(返信するか, 理由, 案リスト) に分解する。

    形式が崩れていたら「返信しない」に倒す。壊れた出力から無理に本文を
    拾って人間に見せると、そのまま送られる事故につながる。
    """
    m = VERDICT_RE.search(text)
    if not m:
        return False, "応答から判定行を読み取れなかった", []
    verdict = m.group(1).strip()

    reason_m = re.search(r"^理由[:：]\s*(.+)$", text, re.MULTILINE)
    reason = reason_m.group(1).strip() if reason_m else ""

    if "返信しない" in verdict:
        return False, reason or verdict, []

    drafts = [d.strip() for _, d in DRAFT_RE.findall(text)]
    drafts = [d for d in drafts if d]
    if not drafts:
        return False, "返信すると判定されたが案を読み取れなかった", []
    return True, reason, drafts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5, help="1 回で起草する件数の上限")
    ap.add_argument("--model", default=os.environ.get("AGY_MODEL") or DEFAULT_MODEL)
    ap.add_argument(
        "--timeout-s", type=int,
        default=int(os.environ.get("AGY_TIMEOUT_S") or DEFAULT_TIMEOUT_S),
    )
    ap.add_argument("--dry-run", action="store_true", help="agy を呼ばずプロンプトを出す")
    ap.add_argument(
        "--redraft", action="store_true",
        help="既に案があるものも作り直す (ペルソナ・プロンプトを直したとき用。既存案は破棄)",
    )
    args = ap.parse_args(argv)

    directory = store.inbox_dir()
    targets = [
        r for r in store.pending(directory)
        if args.redraft or not r.get("drafts")
    ][: args.limit]
    if not targets:
        print("起草対象なし")
        return 0

    drafted = 0
    for rec in targets:
        print(f"\n=== {rec['id']} ({rec['channel']} / {rec['kind']}) ===")
        try:
            persona = load_persona(rec["channel"])
        except DraftError as e:
            print(f"  ペルソナ読み込み失敗: {e}", file=sys.stderr)
            continue

        prompt = build_prompt(rec, persona)
        if args.dry_run:
            print(prompt)
            continue

        try:
            raw = call_agy(prompt, args.model, args.timeout_s)
        except DraftError as e:
            print(f"  起草失敗: {e}", file=sys.stderr)
            continue

        should, reason, drafts = parse_response(raw)
        if not should:
            store.update_record(
                rec["id"], {"status": store.STATUS_IGNORED, "ignore_reason": reason}, directory,
            )
            print(f"  返信しない ({reason})")
            continue

        if args.redraft and rec.get("drafts"):
            # 作り直しは**置き換え**。古い案を残すと、人が PENDING.md で
            # 直った案と没案を並べて見ることになり、没案を送る事故が起きる。
            store.update_record(rec["id"], {"drafts": []}, directory)
        for d in drafts:
            store.add_draft(rec["id"], d, args.model, directory)
        drafted += 1
        print(f"  {len(drafts)} 案を起草")

    if args.dry_run:
        return 0
    # 対象があったのに 1 件も起草できなかったのは、agy かペルソナが壊れている。
    return 0 if drafted or not targets else 1


if __name__ == "__main__":
    sys.exit(main())
