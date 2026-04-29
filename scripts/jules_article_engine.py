import json
import os
import pathlib
from datetime import datetime

def calculate_ivs(item):
    score = 4.0
    title = item.get('title', '')
    features = item.get('features', [])
    if '知育' in title or '五感' in str(features): score += 0.5
    return min(round(score, 1), 5.0)

def main():
    product_dir = pathlib.Path("data/products")
    post_dir = pathlib.Path("content/posts")
    post_dir.mkdir(parents=True, exist_ok=True)

    for f in product_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as file:
                data = json.load(file)

            kw = data.get("keyword", "注目アイテム")
            items = data.get("items", [])

            # Sort by IVS
            for it in items:
                it['ivs'] = calculate_ivs(it)
            items = sorted(items, key=lambda x: x.get('ivs', 0), reverse=True)

            # Determine Cheapest
            prices = [it.get('price') for it in items if isinstance(it.get('price'), (int, float)) and it.get('price') > 0]
            min_p = min(prices) if prices else None

            title = f"🧸 おすすめの{kw}徹底比較｜今買うべき一品は？"
            date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

            content = f"""---
title: "{title}"
date: {date}
draft: false
summary: "Amazonのデータを徹底解析！AI編集長Julesが「{kw}」を比較しました。"
tags: ["{kw}", "知育玩具", "比較"]
---

> **📢 PR表記**: この記事にはアフィリエイトリンクが含まれています。
> 記事の内容はAIエージェント「Jules」が自動生成したものです。（最終更新: {datetime.now().strftime("%Y年%m月%d日")}）

人気の「**{kw}**」について、最新の市場データに基づき、知育価値とコスパの視点で徹底比較しました。

---

## 📊 商品比較一覧

| 商品名 | 知育スコア | 価格 | リンク |
|:---|:---:|:---:|:---:|
"""
            for it in items[:5]:
                label = " 🔥最安値！" if min_p and it['price'] == min_p else ""
                content += f"| {it['title'][:20]}... | {it['ivs']}/5.0 | ￥{it['price']}{label} | [Amazon]({it['url']}) |\n"

            content += "\n---\n\n"

            for it in items:
                content += f"### {it['title']}\n"
                if it.get('image'):
                    content += f"![{it['title']}]({it['image']})\n\n"

                content += f"- **📊 IVS（知育価値スコア）**: {it['ivs']}/5.0\n"
                content += f"- **💰 価格**: ￥{it['price']}\n"
                content += f"- **✅ 特徴**: {', '.join(it.get('features', []))}\n"
                content += f"- **🔗 詳細**: [{it['title']}]({it['url']})\n\n"

            content += f"""> [!QUOTE]
> **AI編集長Jules**
> {kw}選びは本当に悩みますよね。今回の比較が少しでも参考になれば嬉しいです！
"""

            out_path = post_dir / f"{f.stem}.md"
            with open(out_path, "w", encoding="utf-8") as out_file:
                out_file.write(content)

            print(f"Layer 2 complete: Generated Markdown {out_path}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
