import json
import os
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/raw/trends.json")
    parser.add_argument("--out", default="data/queue/")
    args = parser.parse_args()

    if not os.path.exists(args.src):
        print(f"Source {args.src} not found.")
        return

    with open(args.src, "r", encoding="utf-8") as f:
        data = json.load(f)

    queries = data.get("top_queries", [])
    if not queries:
        print("No queries found in trends.")
        return

    os.makedirs(args.out, exist_ok=True)

    # Take the top query and create a task
    top_query = queries[0]["query"]
    slug = top_query.replace(" ", "-")

    task = {
        "mode": "trend",
        "keyword": top_query,
        "asin": ""
    }

    out_file = Path(args.out) / f"trend-{slug}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=4)

    print(f"Created task: {out_file}")

if __name__ == "__main__":
    main()
