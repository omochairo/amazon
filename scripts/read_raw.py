import json, os, pathlib

def load_raw_data(data_dir="data/raw", products_dir="data/products"):
    data = {}

    # 1. Load Raw API Results
    path = pathlib.Path(data_dir)
    if path.exists():
        for f in path.glob("*.json"):
            try:
                data[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except:
                pass

    # 2. Load Individual Product Data (if missing in amazon.json)
    p_path = pathlib.Path(products_dir)
    if p_path.exists():
        product_list = []
        for f in p_path.glob("*.json"):
            try:
                product_list.append(json.loads(f.read_text(encoding="utf-8")))
            except:
                pass

        # Merge products into amazon pool if amazon.json is empty or missing
        if not data.get("amazon"):
            data["amazon"] = {"keyword": "おすすめアイテム", "items": product_list, "mode": "legacy_merge"}
        else:
            # Append new ASINs if not present
            existing_asins = {it.get("asin") for it in data["amazon"].get("items", [])}
            for p in product_list:
                if p.get("asin") not in existing_asins:
                    data["amazon"]["items"].append(p)

    return data

if __name__ == "__main__":
    print(json.dumps(load_raw_data(), ensure_ascii=False, indent=2))
