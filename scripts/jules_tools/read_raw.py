import json, os, pathlib

def load_raw_data(data_dir="data/raw"):
    data = {}
    path = pathlib.Path(data_dir)
    if not path.exists():
        return data

    for f in path.glob("*.json"):
        try:
            data[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except:
            pass
    return data

if __name__ == "__main__":
    print(json.dumps(load_raw_data(), ensure_ascii=False, indent=2))
