import json, os

def get_history(file="data/post_history.json"):
    if os.path.exists(file):
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

if __name__ == "__main__":
    print(json.dumps(get_history(), ensure_ascii=False, indent=2))
