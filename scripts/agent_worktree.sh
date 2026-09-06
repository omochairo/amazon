#!/usr/bin/env bash
# エージェント作業用の隔離 worktree を作る / 片づける (#6602 K6)。
#
# なぜ要るか — 共有の作業ツリーは他人が勝手にブランチを切り替える:
#
#   2026-09-06 の実測。`git reflog` に残っていた checkout の間隔は
#   **7.5 時間で 13 回 = 平均 35 分**。
#
#     19:38  omochairo/re-search-backlog-cap -> main
#     18:55  main -> omochairo/re-search-backlog-cap
#     18:54  sns-publish/verify-6610 -> main
#     ...
#     14:02  feat/k8-ollama-tunnel-snippet-yield -> gsc-snapshot/34006291263  ← 事故
#
#   最後の 1 行が実際の事故。ベンチ測定中に別レーンがツリーを奪い、作業中の
#   スクリプトがツリーから消えて測定が落ちた。
#
# **35 分より長い作業を共有ツリーでやらない。** これは行儀の問題ではなく確率の
# 問題で、長時間の作業は必ず踏む。
#
# 消えるのはファイルだけではない。data/ の中身もブランチごと入れ替わるので、
# **同じコマンドが違う入力で走る**。今回のベンチでは商品集合が変わりかけた
# (バッチ間で比較できなくなる)。落ちてくれる方がまだ良く、静かに違う数字が
# 出るのが最悪。
#
# 使い方:
#   eval "$(scripts/agent_worktree.sh create my-task)"   # $AGENT_WT に cd
#   scripts/agent_worktree.sh path my-task               # パスだけ表示
#   scripts/agent_worktree.sh remove my-task             # 片づけ
#
#   create は「ブランチ feat/<name>」を origin/main から切る。既にあれば
#   そのブランチを再利用する (再開できるように)。
set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: agent_worktree.sh <create|path|remove|list> [name]

  create <name>   origin/main から隔離 worktree を作り、cd 用の eval 文を出す
  path   <name>   worktree のパスだけ出す
  remove <name>   worktree を消す (コミットしていない変更があれば拒否する)
  list            この repo の worktree 一覧
USAGE
    exit 2
}

# 共有ツリーの外に置く。リポジトリ配下に作ると、掃除系の workflow や
# glob (data/articles/*.json 等) が拾ってしまう。
root_dir() {
    printf '%s/omochairo-agent-worktrees' "${TMPDIR:-/tmp}"
}

wt_path() {
    printf '%s/%s' "$(root_dir)" "$1"
}

cmd="${1:-}"
name="${2:-}"
[ -n "$cmd" ] || usage
case "$cmd" in
    list) exec git worktree list ;;
    create|path|remove) [ -n "$name" ] || usage ;;
    *) usage ;;
esac

# 名前はパスとブランチ名の両方になる。変な文字を弾く
case "$name" in
    *[!A-Za-z0-9._-]*|""|.|..) echo "name に使えるのは A-Za-z0-9._- のみ: $name" >&2; exit 2 ;;
esac

path="$(wt_path "$name")"
branch="feat/${name}"

case "$cmd" in
    path)
        printf '%s\n' "$path"
        ;;

    remove)
        # --force は付けない。未コミットの変更を黙って捨てるくらいなら、
        # 人間に見せて止まる方がいい
        git worktree remove "$path"
        echo "removed $path" >&2
        ;;

    create)
        if [ -d "$path" ]; then
            echo "既にある worktree を再利用: $path" >&2
        else
            mkdir -p "$(root_dir)"
            # **必ず origin/main から切る。** 共有ツリーの HEAD は他人の
            # ブランチを指していることがあり、そこから切ると無関係な変更を
            # 抱き込む。fetch も毎回する — origin/main はセッションを跨ぐと古い
            git fetch origin main --quiet
            if git show-ref --verify --quiet "refs/heads/${branch}"; then
                git worktree add "$path" "$branch" >&2
            else
                git worktree add -b "$branch" "$path" origin/main >&2
            fi
        fi
        # eval される前提なので、標準出力に出すのはシェル文だけ
        printf 'export AGENT_WT=%s\n' "$path"
        printf 'cd %s\n' "$path"
        ;;
esac
