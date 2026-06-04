#!/usr/bin/env python3
"""SNS コピー生成対象 ASIN を top-N で選び JSON 出力する (#1580 Part 2 生成側)。

Part 1 の `pick_sns_target.rank_candidates` (知育×価格 加重ランキング) を再利用し、
未投稿かつ **まだ store コピーが無い** 上位 N 件を選んで Jules dispatch 用の
targets JSON を stdout (or --out) に書く。Jules はこの ASIN 群について article 本体
(data/articles/) を自分で読み、data/sns_copy/<ASIN>.json に X/Threads 訴求文を生成する。

「知育×価格」上位＝高単価の既存在庫に偏るため、希少 2 枠/日に実際に出る商品を
先回りでコピー生成しておくのが狙い。selector はランキング順を尊重するので、
配信で実際に選ばれる順 ≒ コピーが用意される順になる。

出力 schema (targets.json):
    {
      "generated_at": "2026-06-04T00:00:00Z",
      "count": 6,
      "targets": [
        {"asin": "B0...", "filename": "2026-05-19-B0....json",
         "edu_axis": 3.9, "best_price": 2600, "score": 0.71, "title": "..."}
      ]
    }

edu_axis / best_price / score は「なぜ選ばれたか」を Jules に伝え、訴求の力点
(知育の高さ / 価格帯) を判断させるために同梱する (配信ロジックには未使用)。

呼び出し例:
    python scripts/select_sns_copy_targets.py --top 6
    python scripts/select_sns_copy_targets.py --top 6 --out /tmp/targets.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pick_sns_target as pst  # noqa: E402
import sns_copy_store  # noqa: E402


def _title_for(filename: str) -> str:
    try:
        data = json.loads((pst.ARTICLES_DIR / filename).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("title") or ""


def select_targets(top: int, include_existing: bool = False) -> list[dict]:
    """未投稿ランキング上位から、store 未生成の ASIN を top 件まで返す。

    include_existing=True なら既存 store コピーが有る ASIN も含める (再生成用)。
    """
    state = pst.load_state()
    published = set(state["published"])
    out: list[dict] = []
    for cand in pst.rank_candidates(published):
        if not include_existing and sns_copy_store.load_copy(cand.asin) is not None:
            continue
        out.append({
            "asin": cand.asin,
            "filename": cand.filename,
            "edu_axis": round(cand.edu_axis, 2) if cand.edu_axis is not None else None,
            "best_price": int(cand.price) if cand.price is not None else None,
            "score": round(cand.score, 4),
            "title": _title_for(cand.filename),
        })
        if len(out) >= top:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=6, help="生成対象の最大件数 (default 6)")
    parser.add_argument("--out", help="出力先ファイル (省略時は stdout)")
    parser.add_argument("--include-existing", action="store_true",
                        help="既に store コピーが有る ASIN も含める (再生成用)")
    args = parser.parse_args()

    targets = select_targets(args.top, include_existing=args.include_existing)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(targets),
        "targets": targets,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"[select] wrote {len(targets)} targets to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
