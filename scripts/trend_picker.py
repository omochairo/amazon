import json
import os
import argparse
import datetime
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
    today = datetime.date.today().isoformat()
    task_id = f"{today}-{slug}"

    # Look for matching ASINs in data/products (simplified for example)
    asins = []
    product_dir = Path("data/products")
    if product_dir.exists():
        for p_file in product_dir.glob("*.json"):
             # In a real scenario, we'd check if the product matches the trend
             asins.append(p_file.stem)

    task = {
        "task_id": task_id,
        "post_type": "comparison",
        "theme": f"{top_query}の最新おすすめ比較",
        "target_age": "全年齢",
        "asins": asins[:3], # Limit to 3 for comparison
        "tone": "丁寧で客観的なレビュー",
        "min_words": 1500,
        "max_words": 2500,
        "seo_keywords": [top_query, "おすすめ", "比較"]
    }

    out_file = Path(args.out) / f"{task_id}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=4)

    print(f"Created task: {out_file}")

if __name__ == "__main__":
    main()
