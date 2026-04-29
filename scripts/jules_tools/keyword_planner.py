import json
import os
import random
from pathlib import Path

QUEUE_FILE = "data/keyword_queue.json"

def get_next_topic():
    if not os.path.exists(QUEUE_FILE):
        # Default seed topics
        return {"mode": "daily_random", "keyword": "知育玩具", "asin": ""}

    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue = json.load(f)

        if not queue:
            return {"mode": "daily_random", "keyword": "知育玩具", "asin": ""}

        topic = queue.pop(0)

        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=4)

        return topic
    except:
        return {"mode": "daily_random", "keyword": "知育玩具", "asin": ""}

def add_to_queue(keyword, mode="daily_random", asin=""):
    queue = []
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue = json.load(f)

    queue.append({"mode": mode, "keyword": keyword, "asin": asin})

    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        add_to_queue(sys.argv[1])
        print(f"Added to queue: {sys.argv[1]}")
    else:
        print(json.dumps(get_next_topic()))
