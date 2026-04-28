import os
import requests
import json
import sys

def fetch_books(keyword):
    # ユーザーが新しく設定した個別の環境変数名に変更
    api_key = os.environ.get("GOOGLEBOOKS_API_KEY")

    # 知育・育児ブログ向けに検索クエリを最適化
    query = f"{keyword} 知育 絵本"

    url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": query,
        "key": api_key,
        "maxResults": 3,
        "langRestrict": "ja",
        "orderBy": "relevance"
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        books = []
        for item in data.get("items", []):
            info = item.get("volumeInfo", {})
            books.append({
                "title": info.get("title"),
                "authors": info.get("authors", ["不明"]),
                "description": info.get("description", "説明なし"),
                "info_url": info.get("infoLink"),
                "image": info.get("imageLinks", {}).get("thumbnail")
            })

        # 実行場所に関わらずプロジェクトルートのdataフォルダを指定
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "books_result.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(books, f, ensure_ascii=False, indent=4)
        print(f"関連書籍を{len(books)}件取得しました: {save_path}")

    except Exception as e:
        print(f"Google Books APIエラー: {e}")

if __name__ == "__main__":
    # 引数がない場合はデフォルトで「知育」を検索
    keyword = sys.argv[1] if len(sys.argv) > 1 else "知育"
    fetch_books(keyword)
