import json

with open("data/articles/2026-08-10-B00LMRFSPC.json", "r", encoding="utf-8") as f:
    data = json.load(f)

data["narrative"]["daily_use"][1] = "メーカー公式では「収納と遊びが一体化した便利なケースです」と記載されており、遊び終わった後はそのままケースに収納でき、お片付け習慣を育むきっかけ作りにも適しています。"

data["claims"][1]["cross_checked"] = False

data["narrative"]["gift_appeal"][2] = "メーカー公式では玩具の安全基準を満たしているとアピールされており、品質面での不安も小さい構成です。"

data["technical_specs"]["other"] = [x for x in data["technical_specs"]["other"] if "STマーク" not in x]
data["technical_specs"]["other"].append("対象年齢 3歳以上")
data["technical_specs"]["other"] = list(set(data["technical_specs"]["other"]))

data["review_signals"]["high_points"].append({
    "text": "遊びの広がりを持たせるパノラマの展開機能",
    "supporting_source_ids": ["src-3"]
})
data["review_signals"]["use_scenes"].append({
    "text": "休日に手持ちのミニカーを並べて遊ぶ時間",
    "supporting_source_ids": ["src-3"]
})

data["claims"].append({
    "claim": "お片付けと遊び場としての機能を備えたケース設計",
    "category": "quality",
    "confidence": "high",
    "supporting_source_ids": ["src-1", "src-3"],
    "cross_checked": True,
    "notes": "商品詳細と実際の遊びの様子から実用性を確認"
})

with open("data/articles/2026-08-10-B00LMRFSPC.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
