import json
import os
from datetime import datetime
from read_raw import load_raw_data
from history_check import get_history
from internal_links import get_related_articles

# Constants
IVS_KEYWORDS = ['知育', 'モンテッソーリ', 'STEM']

def calculate_ivs(item):
    score = 3.8
    name = item.get('name', '')
    features = item.get('features', [])

    # Logic based on attributes
    if len(features) > 3: score += 0.5
    if any(k in name for k in IVS_KEYWORDS): score += 0.4
    if item.get('price', 0) > 5000: score -= 0.2 # Pricey penalty

# Custom modules
from read_raw import load_raw_data

def get_prompt():
    prompt_path = Path("jules/PROMPT_TEMPLATE.md")
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return "You are an AI generating a JSON article based on product data."

def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[Error] OPENAI_API_KEY is missing. Cannot generate article.")
        sys.exit(1)

    raw_data = load_raw_data()
    if not raw_data:
        print("No raw data found in data/raw/ to process.")
        return

    system_prompt = get_prompt()
    user_prompt = f"Here is the raw data from various APIs:\n{json.dumps(raw_data, ensure_ascii=False)}\n\nPlease generate the article JSON."

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-2024-08-06",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content
        article_data = json.loads(content)

        slug = article_data.get("slug", "new-article")
        date_str = datetime.now().strftime('%Y-%m-%d')

        os.makedirs("data/articles", exist_ok=True)
        out_path = f"data/articles/{date_str}-{slug}.json"

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(article_data, f, ensure_ascii=False, indent=4)

        print(f"Generated LLM article JSON: {out_path}")

    except Exception as e:
        print(f"Failed to generate article using LLM: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
