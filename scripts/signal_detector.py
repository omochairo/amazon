import json
import os
import re
from datetime import datetime

DB_PATH = "data/item_db.json"
OUT_PATH = "data/raw/top_signals.json"

def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"history": {}, "last_rankings": {}}

def save_db(db):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=4)

def detect_signals():
    print("--- Starting Layer 2: Signal Detection ---")
    db = load_db()
    history = db.get("history", {})
    last_rankings = db.get("last_rankings", {})

    signals = []

    # 1. Analyze New Arrivals & Pre-orders
    try:
        with open("data/raw/rakuten.json", "r", encoding="utf-8") as f:
            search_data = json.load(f)

        for item in search_data.get("items", []):
            code = item.get("itemCode")
            title = item.get("title", "")

            # Pre-order check
            is_preorder = bool(re.search(r'(予約|発売予定|新発売|\d{4}年\d{1,2}月発売|入荷予定)', title))

            # Collab check
            is_collab = bool(re.search(r'(コラボ|×|公式|ライセンス|記念|限定)', title))

            # First appearance check
            is_new = code not in history

            if is_new or is_preorder:
                score = 0
                if is_new: score += 3
                if is_preorder: score += 5
                if is_collab: score += 4

                signals.append({
                    "type": "new_arrival" if is_new else "preorder",
                    "itemCode": code,
                    "title": title,
                    "url": item.get("url"),
                    "image": item.get("image"),
                    "price": item.get("price"),
                    "score": score
                })

            # Update history
            if code:
                history[code] = {"seen_at": datetime.now().isoformat(), "title": title}

    except FileNotFoundError:
        print("rakuten.json not found, skipping search signals.")

    # 2. Analyze Rankings (Sudden Jumps)
    current_rankings = {}
    try:
        with open("data/raw/rakuten_ranking.json", "r", encoding="utf-8") as f:
            rank_data = json.load(f)

        for item in rank_data.get("items", []):
            code = item.get("itemCode")
            rank = item.get("rank")
            current_rankings[code] = rank
            title = item.get("title", "")

            prev_rank = last_rankings.get(code, 101) # Default to 101 if not in top 100 yesterday
            rank_jump = prev_rank - rank

            if rank_jump > 20 or (prev_rank == 101 and rank <= 30):
                # Sudden jump
                signals.append({
                    "type": "sudden_jump",
                    "itemCode": code,
                    "title": title,
                    "url": item.get("url"),
                    "image": item.get("image"),
                    "price": item.get("price"),
                    "jump": rank_jump,
                    "score": rank_jump * 0.5 + 5 # Base score for jump
                })

    except FileNotFoundError:
        print("rakuten_ranking.json not found, skipping rank signals.")

    # Save current state for tomorrow
    db["history"] = history
    db["last_rankings"] = current_rankings
    save_db(db)

    # Sort signals and output top 1
    if signals:
        signals = sorted(signals, key=lambda x: x["score"], reverse=True)
        top_signal = signals[0]
        print(f"Top Signal Detected: [{top_signal['type']}] Score: {top_signal['score']} - {top_signal['title']}")

        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(top_signal, f, ensure_ascii=False, indent=4)
    else:
        print("No strong signals detected today.")
        # Fallback to a standard keyword if no signals
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump({"type": "standard", "title": "知育玩具", "score": 0}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    detect_signals()
