import os, sys, json, re, pathlib, datetime, requests, logging

def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_rakuten")

JAN_RE = re.compile(r"(?<!\d)(4\d{12}|4\d{7})(?!\d)")


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


def _extract_jan_from_text(text: str) -> str:
    if not text:
        return ""
    m = JAN_RE.search(text)
    return m.group(1) if m else ""


def _match_ranking_item(item: dict, itemcode_idx: dict, jan_idx: dict) -> tuple:
    """ranking item を ASIN にマッチング。返り値: (matched_asin, match_stage)。
    match_stage は 'stage1' (itemCode 直接), 'stage2_jan' (JAN 抽出), '' (未マッチ)。
    """
    code = (item.get("itemCode") or "").strip()
    if code:
        asin = itemcode_idx.get(code)
        if asin:
            return asin, "stage1"
    text = (item.get("itemCaption") or "") + " " + (item.get("title") or "")
    jan = _extract_jan_from_text(text)
    if jan:
        asin = jan_idx.get(jan)
        if asin:
            return asin, "stage2_jan"
    return "", ""

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

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

    resp = requests.get(url, params=params, headers=headers)

    items = []
    if resp.status_code != 200:
        # Search API が落ちても Ranking 取得は独立して続行する (PR1 で sys.exit(1) を除去)
        logger.error(f"Rakuten RMS Search API failed ({url}): {resp.text[:300]}")
        raw_items = []
    else:
        try:
            data = resp.json()
        except ValueError:
            logger.error("Rakuten RMS Search API returned non-JSON")
            data = {}
        raw_items = data.get("Items", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            logger.warning(f"Unexpected Rakuten Search API structure: 'Items' is {type(raw_items)}")
            raw_items = []

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
    try:
        rank_resp = requests.get(ranking_url, params=ranking_params, headers=headers)
        if rank_resp.status_code == 200:
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
        else:
            logger.error(f"Rakuten Ranking API failed: HTTP {rank_resp.status_code} {rank_resp.text[:300]}")
    except Exception as e:
        logger.error(f"Ranking API failed: {e}")

    # --- Stage 1/2 Matching: itemCode 直引き + JAN 抽出 ----------------------
    raw_root = pathlib.Path("data/raw")
    itemcode_idx = _build_itemcode_to_asin(raw_root / "rakuten_matched.json")
    jan_idx = _build_jan_to_asin(raw_root / "per_asin")
    logger.info(f"Match indices: itemCode={len(itemcode_idx)}, jan={len(jan_idx)}")

    stage1_n, stage2_n, unmatched = 0, 0, []
    for it in rank_items:
        asin, stage = _match_ranking_item(it, itemcode_idx, jan_idx)
        it["matched_asin"] = asin or None
        it["match_stage"] = stage or None
        if stage == "stage1":
            stage1_n += 1
        elif stage == "stage2_jan":
            stage2_n += 1
        else:
            unmatched.append({"rank": it.get("rank"), "itemCode": it.get("itemCode"), "title": it.get("title")})

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest = {
        "generated_at": generated_at,
        "genre_id": "566382",
        "input_total": len(rank_items),
        "stage1_matches": stage1_n,
        "stage2_matches": stage2_n,
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
