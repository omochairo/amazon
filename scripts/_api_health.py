"""外部 API の呼び出し結果を run 内で記録する (#8272)。

fetch 系の step は失敗を warning に落として続行するので、外部 API が恒常的に
壊れても run は緑のまま終わる (楽天 Search 20220601 の廃止で 5 週間気付けなかった)。
各 fetcher が呼び出しごとに HTTP ステータスを ``$API_HEALTH_LOG`` (JSONL) へ
追記し、``check_api_health.py`` が最後にまとめて判定する。

``API_HEALTH_LOG`` が未設定なら何もしない (手元実行・GitLab の fetch-data job は
今まで通り)。記録の失敗で fetch 本体を落とさない。
"""
from __future__ import annotations

import json
import os

ENV_VAR = "API_HEALTH_LOG"


def record(api: str, status: int | None) -> None:
    """``api`` の呼び出し 1 回ぶんを記録する。``status`` は HTTP ステータス、
    レスポンスが無い (ネットワークエラー / リトライ枯渇) ときは None。"""
    path = os.environ.get(ENV_VAR)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"api": api, "status": status}) + "\n")
    except OSError:
        pass
