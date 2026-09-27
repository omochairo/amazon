"""check_k8_py38_imports.py

K8 の self-hosted runner (amazon-home-ops の `home` ラベル) は Python 3.8 で、
本リポジトリの scripts を checkout して `python3 -m scripts.<mod>` で実行する。
本リポジトリの CI は 3.11 なので、3.9+ の構文・注釈を入れても CI は緑のまま
K8 のレーンだけが import 時に落ちる。

実例 (#8425): #8163/#8164 で filter_raw_per_asin.py に `set[str]` 注釈が入り、
`from __future__ import annotations` が無かったため、これを import する
23-experience-mining と 46-information-gain-audit が 09-18〜09-26 に止まった。

この script は **Python 3.8 で** 実行する (44-unit-tests.yml の k8-py38-imports job)。
  1. K8_MODULES を全部 import する (モジュール読込時の 3.9+ 依存を検出)
  2. --closure-out に、import で読み込まれた本リポジトリ内のファイル一覧を書く
     (job 側で vermin に渡し、関数内で実行時に初めて落ちる 3.9+ 機能を検出する)
"""
from __future__ import annotations

import argparse
import importlib
import pathlib
import sys
import traceback

# amazon-home-ops の self-hosted workflow が `python3 -m` / import 検査で呼ぶ
# モジュール (2026-09-27 時点で grep した 22 件)。home-ops は private なので
# ここに写しを置く。**home-ops 側でレーンを足したらここにも足すこと。**
K8_MODULES = (
    "scripts.append_information_gain_history",
    "scripts.append_uniqueness_audit_history",
    "scripts.audit_information_gain",
    "scripts.audit_query_entailment",
    "scripts.audit_site_health",
    "scripts.audit_uniqueness",
    "scripts.build_wp_navi_link_candidates",
    "scripts.build_wp_wp_h2_link_candidates",
    "scripts.comment_answerability_audit",
    "scripts.comment_information_gain_audit",
    "scripts.comment_uniqueness_audit",
    "scripts.compute_semantic_related",
    "scripts.crawl_yahoo_reviews",
    "scripts.detect_demand_gaps",
    "scripts.fetch_google_suggest",
    "scripts.fetch_price_watch",
    "scripts.fetch_suggest_info",
    "scripts.generate_faq_seo",
    "scripts.generate_internal_links",
    "scripts.judge_ambiguous_products",
    "scripts.mine_experience",
    "scripts.run_lighthouse_lane",
)


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path("."))
    p.add_argument("--closure-out", type=pathlib.Path, default=None)
    args = p.parse_args(argv)

    root = args.repo_root.resolve()
    # K8 と同じく repo root と scripts/ の両方から解決できるようにする
    # (scripts 内に `import _fetch_targets` のような素の import がある)。
    sys.path[:0] = [str(root), str(root / "scripts")]

    failed = []
    for mod in K8_MODULES:
        if not (root / (mod.replace(".", "/") + ".py")).exists():
            print(f"::error::{mod} is listed in K8_MODULES but does not exist")
            failed.append(mod)
            continue
        try:
            importlib.import_module(mod)
        except SystemExit:
            pass
        except BaseException:  # noqa: BLE001 — 何で落ちても K8 では止まる
            print(f"::error::{mod} is not importable on Python "
                  f"{sys.version_info.major}.{sys.version_info.minor}")
            traceback.print_exc()
            failed.append(mod)

    if args.closure_out is not None:
        files = set()
        for m in list(sys.modules.values()):
            f = getattr(m, "__file__", None)
            if not f:
                continue
            path = pathlib.Path(f).resolve()
            if root in path.parents and path.suffix == ".py":
                files.add(path.relative_to(root).as_posix())
        args.closure_out.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")
        print(f"closure: {len(files)} files -> {args.closure_out}")

    print(f"imported {len(K8_MODULES) - len(failed)}/{len(K8_MODULES)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
