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

    # 2. Load Individual Product Data (Only as fallback)
    p_path = pathlib.Path(products_dir)
    if p_path.exists() and not data.get("amazon"):
        product_list = []
        for f in p_path.glob("*.json"):
            try:
                product_list.append(json.loads(f.read_text(encoding="utf-8")))
            except:
                pass

        if product_list:
            data["amazon"] = {"keyword": "おすすめアイテム", "items": product_list, "mode": "legacy_merge"}

    return data

if __name__ == "__main__":
    print(json.dumps(load_raw_data(), ensure_ascii=False, indent=2))
