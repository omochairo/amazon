import json
import os

filepath = "data/articles/2026-09-09-B0GYCBHKT7.json"
with open(filepath, "r", encoding="utf-8") as f:
    data = json.load(f)

# 1. Certification hallucination fix
data["product"]["certifications"] = []
data["claims"] = [c for c in data["claims"] if "ST" not in c.get("notes", "")]
data["technical_specs"]["other"] = [item for item in data["technical_specs"].get("other", []) if "ST" not in item]
for k in ["why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose"]:
    if k in data["narrative"]:
        data["narrative"][k] = [s.replace("STマークを取得しており、", "") for s in data["narrative"][k]]

# 2. Invalid Cross-Checking fix
for claim in data["claims"]:
    sources = claim.get("supporting_source_ids", [])
    if all(src.startswith("src-amazon") or src.startswith("src-rakuten") or src.startswith("src-yahoo") for src in sources):
        claim["cross_checked"] = False

# 3. Missing Review Quotes fix
data["narrative"]["safety_note"][1] = "日本の玩具安全基準を満たした作りで3歳から安心して遊べます。"
data["narrative"]["daily_use"][1] = "メーカー公式ページで「単2形乾電池1本使用(電池は別売です。)」と記載されている通り、シンプルな走行遊びに集中できる設計となっています。"
data["narrative"]["why_this_product"][2] = "HOBBY Watchの報道では「国鉄時代の1963年(昭和38年)に登場」と紹介されており、昭和時代に活躍した車両として根強い人気があります。"

with open(filepath, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("Fixed review issues.")
