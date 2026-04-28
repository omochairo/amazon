import os
import json
import sys
from pytrends.request import TrendReq

def main(keyword):
    print(f"--- Google Trends Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    try:
        pytrends = TrendReq(hl='ja-JP', tz=540)

        # 関連キーワードの取得
        pytrends.build_payload([keyword], cat=0, timeframe='today 5-y', geo='JP', gprop='')
        related_queries = pytrends.related_queries()

        top_queries = []
        if keyword in related_queries and 'top' in related_queries[keyword]:
            df_top = related_queries[keyword]['top']
            if df_top is not None:
                for index, row in df_top.head(5).iterrows():
                    print(f"Trend Query: {row['query']} (Value: {row['value']})")
                    top_queries.append({
                        "query": row['query'],
                        "value": int(row['value'])
                    })

        results = {
            "keyword": keyword,
            "top_queries": top_queries
        }

        os.makedirs("data", exist_ok=True)
        with open("data/trends_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved trends data to data/trends_result.json")

    except Exception as e:
        print(f"An error occurred during Google Trends Fetch: {e}")
        # Pytrendsはたまに429などの制限に引っかかるので、エラーでも空データを保存して続行できるようにする
        results = {"keyword": keyword, "top_queries": []}
        with open("data/trends_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_trends.py <keyword>")
