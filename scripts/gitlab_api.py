"""GitLab API の最小ヘルパー (データ更新 MR 用)。

元は scripts/invoke_jules_repoless.py (GitLab 側の Jules 記事生成) の中にあった。
Jules の終了に伴いそちらを削除したので、scripts/create_data_mr.py が使う
``call`` と ``enable_auto_merge`` だけをここへ移した (amazon-navi-brain#170)。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def _http(url, token_header, data=None, method=None, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **token_header},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


class GitLab:
    def __init__(self):
        self.token = os.environ["GITLAB_API_TOKEN"]
        pid = os.environ.get("CI_PROJECT_ID", "84175362")
        self.base = f"https://gitlab.com/api/v4/projects/{pid}"

    def _hdr(self):
        return {"PRIVATE-TOKEN": self.token}

    def call(self, path, data=None, method=None):
        return _http(self.base + path, self._hdr(), data, method)

    def enable_auto_merge(self, mr_iid):
        """MWPS (merge when pipeline succeeds) を設定 (旧 05 の gh pr merge --auto 相当)。

        MR 直後は pipeline 未作成で 405/406 が返るため、リトライで待つ。
        validate-article (MR パイプライン) 成功時に squash マージされる。"""
        for attempt in range(12):  # 最大 ~2 分
            try:
                self.call(f"/merge_requests/{mr_iid}/merge",
                          data={"merge_when_pipeline_succeeds": True,
                                "squash": True, "should_remove_source_branch": True},
                          method="PUT")
                return True
            except urllib.error.HTTPError as e:
                # 405/406: pipeline 未作成。422: mergeability チェック中 (実測 MR !7)。
                if e.code in (405, 406, 422):
                    time.sleep(10)
                    continue
                print(f"warning: auto-merge setup failed for !{mr_iid}: HTTP {e.code}",
                      file=sys.stderr)
                return False
        print(f"warning: auto-merge setup timed out for !{mr_iid} (要手動 merge)",
              file=sys.stderr)
        return False
