"""楽天市場 商品 / ランキングデータ取得スクリプト

🚨 重要 — 使用 API: **楽天ウェブサービス v20220601 (新エンドポイント)**
  - 旧 endpoint (``app.rakuten.co.jp``) は **使えない** (session 58 で trap)。
  - 必ず ``openapi.rakuten.co.jp`` ホスト + URL 末尾の ``/20220601`` バージョン。
  - 認証は ``applicationId`` クエリパラメータ (環境変数 ``RAKUTEN_APP_ID``)。
  - PA-API 5 / Amazon Creator API とは別系統の楽天独自 API。

エンドポイント:
  - 商品検索: ``GET https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601``
  - ランキング: ``GET https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601``
  - 共通必須パラメータ:
      ``applicationId``: RAKUTEN_APP_ID (環境変数)
      ``formatVersion``: 2  (items が flat list で返る; v1 は dict-wrap)

関連 trap:
  [[omochairo-rakuten-v20220601-trap]] — 3 段 trap:
    1. ``version=20220601`` クエリパラメータではなく URL 末尾必須
    2. ``applicationId`` 必須 (``accessKey`` ではない)
    3. ホストは ``openapi.rakuten.co.jp`` (``app.rakuten.co.jp`` は廃止)

環境変数:
    RAKUTEN_APP_ID: 楽天ウェブサービスのアプリケーション ID (必須)
"""
import os, sys, json, re, time, pathlib, datetime, requests, logging

def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_rakuten")

JAN_RE = re.compile(r"(?<!\d)(4\d{12}|4\d{7})(?!\d)")

# Rakuten Webservice TPS=1。Search 5 連打 → Ranking で 429 を踏んだ過去あり
# (run 26849869200, 2026-06-02)。Ranking call 前の最低保険として 1.5s 空ける。
RAKUTEN_API_GAP_SEC = 1.5


def _rakuten_get_with_retry(url: str, params: dict, headers: dict, label: str,
                            max_retries: int = 1, backoff_sec: float = 3.0):
    """楽天 API を 429 のときだけ 1 回リトライして返す。それ以外は素通し。

    429 は TPS=1 制限超過なので backoff 後に通る確率が高い。リトライしないと
    weekly.json が空で上書きされ /ranking/ が「お待ちください」状態になる
    (Issue: #1454 で発生)。
    """
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
        except requests.exceptions.RequestException as e:
            logger.error(f"Rakuten {label} request failed (attempt {attempt + 1}): {e}")
            return None
        if resp.status_code == 429 and attempt < max_retries:
            logger.warning(
                f"Rakuten {label} got HTTP 429 — backing off {backoff_sec}s and retrying"
            )
            time.sleep(backoff_sec)
            continue
        return resp
    return None


def _build_itemcode_to_asin(matched_path: pathlib.Path) -> dict:
    if not matched_path.exists():
        return {}
    try:
        data = json.loads(matched_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    index = {}
    for it in data.get("items", []):
        code = (it.get("itemCode") or "").strip()
        asin = (it.get("matched_asin") or "").strip()
        if code and asin:
            index[code] = asin
    return index


def _build_jan_to_asin(per_asin_root: pathlib.Path) -> dict:
    index: dict[str, str] = {}
    if not per_asin_root.exists():
        return index
    for snap_path in per_asin_root.glob("*/amazon.json"):
        asin = snap_path.parent.name
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        item = snap.get("item") if isinstance(snap, dict) else None
        if not isinstance(item, dict):
            continue
        jan = (item.get("jan_code") or "").strip()
        if jan:
            index.setdefault(jan, asin)
    return index


# 記事スラッグ末尾 10 文字 ASIN (B0... / ISBN-10 数字 ASIN 両対応)。
_ARTICLE_ASIN_RE = re.compile(r"-([A-Z0-9]{10})$", re.IGNORECASE)
# サイドカー JSON (.enrichment/.seo/.quality) は記事本体ではないので除外する
# ([[omochairo-sidecar-json-shadow]] / build_post の SUFFIX_SKIP と同方針)。
_ARTICLE_SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")


def _build_article_asins(articles_root: pathlib.Path) -> set:
    """data/articles/<YYYY-MM-DD-ASIN>.json から記事化済み ASIN 集合を構築。

    Issue #1149: ランキング rematch で matched_asin が立っても /products/<asin>/ の
    記事が無ければ 404 になるため、内部リンクは「記事ありき」に gate する。

    Issue #600 follow-up: 記事ソースは **commit 済みの data/articles/*.json** を読む。
    ビルド時生成物の hugo/content/posts/*.md は .gitignore (/hugo) 対象で、fetch/sniper
    workflow のランナー checkout には存在しない。そこを読んでいたため has_article が CI で
    常に False になり、記事ゲートが全マッチを落として本番の Amazon CTA/内部リンク/履歴
    チャートが恒常的に出ていなかった (resolve_ranking_asins._load_article_asins と同一規約)。
    """
    asins: set[str] = set()
    if not articles_root.is_dir():
        return asins
    for p in articles_root.glob("*.json"):
        if p.stem.endswith(_ARTICLE_SIDECAR_SUFFIXES):
            continue
        m = _ARTICLE_ASIN_RE.search(p.stem)
        if m:
            asins.add(m.group(1).upper())
    return asins


def _extract_jan_from_text(text: str) -> str:
    if not text:
        return ""
    m = JAN_RE.search(text)
    return m.group(1) if m else ""


def _match_ranking_item(
    item: dict,
    itemcode_idx: dict,
    jan_idx: dict,
    article_asins: set | None = None,
) -> tuple:
    """ranking item を ASIN にマッチング。返り値: (matched_asin, match_stage, has_article)。

    match_stage は 'stage1' (itemCode 直接), 'stage2_jan' (JAN 抽出), '' (未マッチ)。

    Issue #600 follow-up: itemCode/JAN が解決できれば **記事が無くても** matched_asin を
    返す。Amazon 外部 CTA (amazon.co.jp/dp/<asin>) は記事を必要としないため、記事化前でも
    リンクを先行表示できる。``article_asins`` を渡した場合は ``has_article`` で記事有無を
    区別し、内部 /products/<asin>/ リンク (= 404 リスク) は呼び出し側 (テンプレ) が
    このフラグで gate する (#1149 の 404 防止意図はテンプレ側に移譲)。
    ``article_asins=None`` なら has_article=True (後方互換)。
    """
    def _has_article(asin: str) -> bool:
        return article_asins is None or asin.upper() in article_asins

    code = (item.get("itemCode") or "").strip()
    if code:
        asin = itemcode_idx.get(code)
        if asin:
            return asin, "stage1", _has_article(asin)
    text = (item.get("itemCaption") or "") + " " + (item.get("title") or "")
    jan = _extract_jan_from_text(text)
    if jan:
        asin = jan_idx.get(jan)
        if asin:
            return asin, "stage2_jan", _has_article(asin)
    return "", "", False


def _match_all(rank_items: list, itemcode_idx: dict, jan_idx: dict, article_asins: set | None) -> tuple:
    """rank_items を一括マッチングし、(stage1_n, stage2_n, unmatched_list) を返す。
    rank_items の各 dict に matched_asin / match_stage を破壊的に書き込む。
    """
    stage1_n, stage2_n, unmatched = 0, 0, []
    for it in rank_items:
        asin, stage, has_article = _match_ranking_item(it, itemcode_idx, jan_idx, article_asins)
        it["matched_asin"] = asin or None
        it["match_stage"] = stage or None
        # Issue #600 follow-up: 記事の有無を別フィールドで保持。テンプレは matched_asin で
        # Amazon CTA を、has_article で内部 /products/<asin>/ リンクを gate する。
        it["has_article"] = bool(asin) and has_article
        if stage == "stage1":
            stage1_n += 1
        elif stage == "stage2_jan":
            stage2_n += 1
        else:
            unmatched.append({
                "rank": it.get("rank"),
                "itemCode": it.get("itemCode"),
                "title": it.get("title"),
            })
    return stage1_n, stage2_n, unmatched


def _rematch_only(out_dir: pathlib.Path) -> int:
    """Issue #1149: 既存 weekly.json を読み、API を叩かずに現行 indices で再マッチ。
    返り値: 終了コード (0=成功, 2=weekly.json 不在)。
    """
    weekly_path = pathlib.Path("hugo/data/ranking/weekly.json")
    if not weekly_path.exists():
        logger.error(f"rematch-only: {weekly_path} not found")
        return 2
    payload = json.loads(weekly_path.read_text(encoding="utf-8"))
    rank_items = payload.get("items", [])

    # weekly.json が既に空 (= 直前の通常 fetch が API 失敗で空のまま flush された / 復旧前)
    # の状態で rematch すると、空のまま rematched_at だけ更新して履歴を 1 周分塗り潰すので
    # スキップする。fetch_rakuten 側の guard とセットで /ranking/ 表示を保護する。
    if not rank_items:
        logger.warning(
            f"rematch-only: weekly.json has 0 items — skipping rematch to preserve previous state"
        )
        return 0

    raw_root = pathlib.Path("data/raw")
    itemcode_idx = _build_itemcode_to_asin(raw_root / "rakuten_matched.json")
    jan_idx = _build_jan_to_asin(raw_root / "per_asin")
    article_asins = _build_article_asins(pathlib.Path("data/articles"))
    logger.info(
        f"rematch-only indices: itemCode={len(itemcode_idx)}, jan={len(jan_idx)}, "
        f"articles={len(article_asins)}"
    )

    stage1_n, stage2_n, unmatched = _match_all(rank_items, itemcode_idx, jan_idx, article_asins)

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload["items"] = rank_items
    payload["rematched_at"] = generated_at
    manifest = {
        "generated_at": generated_at,
        "genre_id": payload.get("genre_id") or "566382",
        "input_total": len(rank_items),
        "stage1_matches": stage1_n,
        "stage2_matches": stage2_n,
        "matched_with_article": sum(1 for it in rank_items if it.get("has_article")),
        "unmatched": len(unmatched),
        "unmatched_items": unmatched,
        "rematch_only": True,
    }
    logger.info(
        f"rematch-only result: total={len(rank_items)} "
        f"stage1={stage1_n} stage2_jan={stage2_n} unmatched={len(unmatched)}"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rakuten_ranking.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    (out_dir / "_rakuten_ranking_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    weekly_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (weekly_path.parent / "_match_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--search-pages", type=int, default=1,
                        help="Rakuten Ichiba Search pages to fetch (hits=30/page). #810 Phase 2: "
                             "sniper raises this to widen the JAN discovery pool beyond the 30-item ranking.")
    parser.add_argument("--rematch-only", action="store_true",
                        help="Issue #1149: skip API, re-match existing hugo/data/ranking/weekly.json "
                             "against current per_asin / rakuten_matched / article indices. Used after "
                             "sniper backfill so newly-created articles get linked from /ranking/ "
                             "without waiting for next Tuesday's weekly cron.")
    args = parser.parse_args()

    if args.rematch_only:
        rc = _rematch_only(pathlib.Path(args.out))
        sys.exit(rc)

    app_id = get_secret("RAKUTEN_APP_ID")
    access_key = get_secret("RAKUTEN_ACCESS_KEY")
    aff_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()

    if not app_id or not access_key:
        logger.warning("Rakuten API keys missing. Generating mock test data for Rakuten.")
        os.makedirs(args.out, exist_ok=True)
        items = [{
            "title": "[テストデータ] 楽天モック知育玩具ブロック",
            "price": 3500,
            "url": "https://hb.afl.rakuten.co.jp/hgc/mock/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fmock%2Fitem%2F",
            "image": "https://via.placeholder.com/300x300.png?text=Rakuten+Mock",
            "source": "Rakuten (Mock)"
        }]
        with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": items}, f, ensure_ascii=False, indent=4)
        return

    # --- Fetch Search Data (Layer 1) ---
    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    headers = {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon"
    }
    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": args.keyword,
        "genreId": "566382",
        "sort": "-updateTimestamp",
        "formatVersion": 2,
        "hits": 30
    }
    if aff_id: params["affiliateId"] = aff_id

    # #810 Phase 2: ページングで Search プールを広げ、ランキング 30 件の外にある
    # 売れ筋 JAN も resolve_ranking_asins の解決対象に供給する。Search API が落ちても
    # Ranking 取得は独立して続行する (PR1 で sys.exit(1) を除去)。
    items = []
    raw_items = []
    search_pages = max(1, args.search_pages)
    for page in range(1, search_pages + 1):
        params["page"] = page
        try:
            resp = requests.get(url, params=params, headers=headers)
        except requests.exceptions.RequestException as e:
            logger.error(f"Rakuten RMS Search API request failed page={page}: {e}")
            break
        if resp.status_code != 200:
            logger.error(f"Rakuten RMS Search API failed page={page} ({url}): {resp.text[:300]}")
            break
        try:
            data = resp.json()
        except ValueError:
            logger.error("Rakuten RMS Search API returned non-JSON")
            break
        page_items = data.get("Items", []) if isinstance(data, dict) else []
        if not isinstance(page_items, list):
            logger.warning(f"Unexpected Rakuten Search API structure: 'Items' is {type(page_items)}")
            break
        if not page_items:
            break  # ページ尽き
        raw_items.extend(page_items)
        if page < search_pages:
            time.sleep(1.1)  # Rakuten TPS=1 (旧 0.4 は不足、429 を踏む)
    logger.info(f"Rakuten Search: fetched {len(raw_items)} items across up to {search_pages} page(s)")

    for item in raw_items:
        i = item.get("Item", item) if isinstance(item, dict) else {}

        # Robust image extraction
        image_url = ""
        img_list = i.get("mediumImageUrls", [])
        if isinstance(img_list, list) and len(img_list) > 0:
            first_img = img_list[0]
            if isinstance(first_img, dict):
                image_url = first_img.get("imageUrl", "")
            elif isinstance(first_img, str):
                image_url = first_img

        items.append({
            "title": i.get("itemName", "Unknown Rakuten Product"),
            "price": i.get("itemPrice", 0),
            "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
            "image": image_url,
            "itemCode": i.get("itemCode", ""),
            # #810 Phase 2: itemCaption は JAN/型番を含むことが多く、resolve_ranking_asins
            # の _extract_jan_from_text が新規 ASIN 解決のためにここから JAN を抽出する。
            "itemCaption": i.get("itemCaption", ""),
            "reviewCount": i.get("reviewCount", 0),
            "source": "Rakuten"
        })

    # --- Fetch Ranking Data (Layer 1) ---
    # Use the openapi host (RMS-style) for v20220601. The legacy
    # app.rakuten.co.jp host returns "specify valid applicationId" for
    # v20220601 with our app_id, mirroring how Search migrated above.
    ranking_url = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"
    ranking_params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "genreId": "566382", # Toys genre
        "formatVersion": 2
    }
    if aff_id: ranking_params["affiliateId"] = aff_id

    rank_items = []
    # Search 連打の直後は TPS=1 を踏むので Ranking 前に空ける。
    time.sleep(RAKUTEN_API_GAP_SEC)
    try:
        rank_resp = _rakuten_get_with_retry(
            ranking_url, ranking_params, headers, label="Ranking"
        )
        if rank_resp is not None and rank_resp.status_code == 200:
            rank_data = rank_resp.json()
            raw_rank_items = rank_data.get("Items", [])
            if not isinstance(raw_rank_items, list):
                logger.warning(f"Unexpected Rakuten Ranking API structure: 'Items' is {type(raw_rank_items)}")
                raw_rank_items = []

            for item in raw_rank_items:
                i = item.get("Item", item) if isinstance(item, dict) else {}

                image_url = ""
                img_list = i.get("mediumImageUrls", [])
                if isinstance(img_list, list) and len(img_list) > 0:
                    first_img = img_list[0]
                    if isinstance(first_img, dict):
                        image_url = first_img.get("imageUrl", "")
                    elif isinstance(first_img, str):
                        image_url = first_img

                rank_items.append({
                    "rank": i.get("rank"),
                    "title": i.get("itemName", "Unknown Rank Product"),
                    "price": i.get("itemPrice", 0),
                    "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
                    "image": image_url,
                    "itemCode": i.get("itemCode", ""),
                    "itemCaption": i.get("itemCaption", ""),
                    "reviewCount": i.get("reviewCount", 0),
                    "shopName": i.get("shopName", ""),
                })
        elif rank_resp is not None:
            logger.error(f"Rakuten Ranking API failed: HTTP {rank_resp.status_code} {rank_resp.text[:300]}")
        else:
            logger.error("Rakuten Ranking API failed: no response (network / retry exhausted)")
    except Exception as e:
        logger.error(f"Ranking API failed: {e}")

    # --- Stage 1/2 Matching: itemCode 直引き + JAN 抽出 ----------------------
    raw_root = pathlib.Path("data/raw")
    itemcode_idx = _build_itemcode_to_asin(raw_root / "rakuten_matched.json")
    jan_idx = _build_jan_to_asin(raw_root / "per_asin")
    article_asins = _build_article_asins(pathlib.Path("data/articles"))
    logger.info(
        f"Match indices: itemCode={len(itemcode_idx)}, jan={len(jan_idx)}, "
        f"articles={len(article_asins)}"
    )

    stage1_n, stage2_n, unmatched = _match_all(rank_items, itemcode_idx, jan_idx, article_asins)

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest = {
        "generated_at": generated_at,
        "genre_id": "566382",
        "input_total": len(rank_items),
        "stage1_matches": stage1_n,
        "stage2_matches": stage2_n,
        "matched_with_article": sum(1 for it in rank_items if it.get("has_article")),
        "unmatched": len(unmatched),
        "unmatched_items": unmatched,
    }
    logger.info(
        f"Rakuten Ranking match: total={len(rank_items)} "
        f"stage1={stage1_n} stage2_jan={stage2_n} unmatched={len(unmatched)}"
    )

    # --- Write outputs --------------------------------------------------------
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": args.keyword, "items": items}, f, ensure_ascii=False, indent=4)

    # signal_detector が読む既存パス。matched_asin/match_stage は追加フィールド (後方互換)
    ranking_payload = {
        "generated_at": generated_at,
        "source": "Rakuten Ichiba Ranking API",
        "genre_id": "566382",
        "items": rank_items,
    }

    # rank_items が空 (= Ranking API が 429/エラーで失敗 or 0 件返却) のとき weekly.json /
    # _match_manifest.json / rakuten_ranking.json を上書きしない。これがないと /ranking/
    # が「次回 cron をお待ちください」状態に陥り、復旧まで前回 cron run の間データ消失する
    # (Issue: run 26849869200 = #1454 で実発生)。raw/ 側の rakuten_ranking.json と manifest
    # は signal_detector / sniper の入力なので同じ保護を適用する。
    if not rank_items:
        logger.warning(
            "Ranking API returned 0 items — preserving previous weekly.json / manifests "
            "(no overwrite). /ranking/ will continue to show last successful snapshot."
        )
    else:
        with open(os.path.join(args.out, "rakuten_ranking.json"), "w", encoding="utf-8") as f:
            json.dump(ranking_payload, f, ensure_ascii=False, indent=4)
        # Observability: PR #677 と同方針の build/match manifest
        with open(os.path.join(args.out, "_rakuten_ranking_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        # Hugo Data Templates 用に同データを hugo/data/ranking/ へも複製する。
        # `/hugo` は .gitignore 対象だが weekly.json を 1 回 force-add で tracked にしておき、
        # 以降は通常の git add で更新可能 (cron workflow は git add -f で初回作成も吸収)。
        hugo_ranking_dir = pathlib.Path("hugo/data/ranking")
        hugo_ranking_dir.mkdir(parents=True, exist_ok=True)
        (hugo_ranking_dir / "weekly.json").write_text(
            json.dumps(ranking_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (hugo_ranking_dir / "_match_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
