import json

filepath = "data/articles/2026-05-18-B0C1Y5WJYQ.json"
with open(filepath, "r") as f:
    data = json.load(f)

# Keep only src-1, src-2, src-3
data["sources"] = data["sources"][:3]

# Keep only claims 1 and 2, which use src-1
data["claims"] = data["claims"][:2]

with open(filepath, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
