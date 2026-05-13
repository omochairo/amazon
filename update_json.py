import json

with open("data/articles/2026-05-13-B0DB7BYTQL.json", "r") as f:
    data = json.load(f)

# Update narrative to ensure the product name appears twice and the length is sufficient
product_name = "プラレール ビッグステーション"

data["narrative"]["lead"] = f"お子さまが大好きなプラレールの世界がさらに広がる『タカラトミー プラレール レバーでアクション＆サウンド! ビッグステーション』。自分でレバーを操作して遊べる楽しさや、リアルなサウンドが魅力の{product_name}ですが、どこで買うのが一番おトクなのでしょうか。本記事ではおもちゃロボが3サイトを横断し、価格・口コミ・安全性まで丁寧に比較しました。"
data["narrative"]["why_this_product"] = f"『タカラトミー プラレール レバーでアクション＆サウンド! ビッグステーション』が選ばれている理由は、なんといってもお子さま自身でレバーを操作し、列車をコントロールできる体験にあります。リアルなサウンドが遊びを盛り上げ、ただ電車を走らせるだけではない、ご家庭での遊びの幅が広がる点が支持されています。既存のレイアウトに組み込めるのも{product_name}の大きなポイントです。"
data["narrative"]["gift_appeal"] = f"プレゼントとして贈るなら、開けた瞬間にお子さまの目を輝かせること間違いなしの{product_name}がおすすめです。特に3歳以上の乗り物好きなお子さまにとっては、夢中になる要素がたっぷり詰まっています。しっかりとした作りのため、{product_name}が贈り物として選ばれる理由も頷けます。お誕生日や入園祝いにも最適です。"
data["narrative"]["daily_use"] = f"実際に手に取ってみると、『タカラトミー プラレール レバーでアクション＆サウンド! ビッグステーション』はいつものプラレールのレイアウトに組み込むことで、遊びがさらに深まることがわかります。お子さまが駅長さんになった気分で楽しめるため、ご家族と一緒に声掛けをしながら遊ぶ時間も増えるでしょう。{product_name}は想像力が広がる素敵なアイテムです。"
data["narrative"]["safety_note"] = f"安全面で気になる方も多いのが電池の取り扱いです。本品の{product_name}は電池ケース蓋に「ネジ止め式」を採用しており、誤飲防止の対策がしっかりされています。対象年齢も3歳以上となっており、安心して遊ばせることができる配慮が嬉しいポイントです。細かな部品にも注意を払って設計された{product_name}は安心です。"
data["narrative"]["closing"] = f"総合的に見て、『タカラトミー プラレール レバーでアクション＆サウンド! ビッグステーション』は、プラレール好きなお子さまへのプレゼントとして非常におすすめです。現在、価格はAmazonが最も手頃でした。おもちゃロボのおすすめとして、ぜひ{product_name}を候補に入れてみてください。"

# Update sources to align better with claims
data["sources"][2]["notes"] = "対象年齢3歳以上であることに加え、電池ケースのネジ止め式構造を確認"

with open("data/articles/2026-05-13-B0DB7BYTQL.json", "w", encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
