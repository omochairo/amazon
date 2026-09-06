# agy から出典 URL を取れるか — probe 記録

`scripts/probe_agy_sources.py` の実測記録（2026-09-06 / agy 1.1.27 /
`gemini-3.8-flash-low`）。**実装はまだしていない。** 実装前に成立条件を潰すための probe。

## 何を確かめたかったか

`gather_antigravity` は `source_url: ""` を返している。**このレーンだけ出所が
辿れない。** 素材としては使えても、E-E-A-T の裏付け（#2699 柱0）にも後からの検証にも
使えない。ここに実 URL を入れられるか。

元の構想は「agy には検索と抜粋だけさせ、日本語化はローカル gemma に寄せる」だったが、
probe の過程で **抜粋は取れない**ことが分かり、実現可能なのは「要約 + 出典 URL」だけ
だと判明した。

## 先に潰れた前提

### 1. `read_url` は headless で使えない → 原文抜粋は不可能

「原文のまま抜粋しろ」と頼むと agy は個別ページを開こうとし、`read_url` 権限が
headless で auto-deny される:

```
jetski: no output produced — a tool required the "read_url" permission that
headless mode cannot prompt for, so it was auto-denied.
{"status":"CANCELED","response":"", ... "denied_actions":[{"action":"read_url"}]}
```

**検索結果の範囲で完結させるしかない。** 権限を開ける手段はあるが agy の信頼境界を
広げるので owner 判断が要る（`--dangerously-skip-permissions` は全ツール自動承認なので
論外。`permissions.allow` に `read_url` を絞って足す方はありうる）。

検索スニペットだけで抜粋させる `excerpt_url` も試したが、**grounding が 0.75 → 0.31 に
崩壊**した（商品から話が逸れる）。抜粋路線は打ち切り。

### 2. `--json-schema` は Web 検索と併用できない

ツールを使わないプロンプトでは `structured_output` が返る:

```
--print="2たす3は? answer に日本語、n に数値"
→ "structured_output":{"answer":"五","n":5}
```

しかし検索グラウンディングが走ると schema は無視され散文が返る。
**構造化出力に頼れないので URL は本文からパースする。**

### 3. URL は grounding redirect で返る — ただし解決できる

```
https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ...
  → 302 → https://product.rakuten.co.jp/product/-/147c52a9cc512b7e559f3b57271a6d8c/
```

不透明なトークン URL だが **302 を辿れば実 URL に解決できる**。
収集時に解決して保存すれば traceability は成立する。ここがダメなら案ごと潰れていた。

## 実測（4 プロンプト × 3 商品 × 3 trial）

| プロンプト | ok | score | grounding | **balance** | 注意点数 | 本文長 | URL/call | http200 | ドメイン | p50 |
|---|---|---|---|---|---|---|---|---|---|---|
| `summary`（現行・対照） | 1.00 | 0.824 | 0.75 | 0.56 | 0.67 | 384 | 0.00 | — | 0 | 13.6s |
| `summary_url` | 1.00 | 0.735 | 0.75 | **0.11** | 0.11 | 356 | 3.56 | 0.81 | 14 | 23.9s |
| **`summary_url_balanced`** | **0.89** | **0.912** | 0.75 | **1.00** | **2.00** | 385 | **3.88** | **0.87** | 15 | 37.7s |
| `excerpt_url` | 1.00 | 0.601 | **0.31** | 0.22 | 0.33 | 369 | 3.11 | 0.86 | 15 | 28.5s |

### 出典を求めると注意点が消える、が指示すれば戻る

`summary_url` で **balance が 0.56 → 0.11 に崩壊**した。URL が出力予算を食って
注意点が押し出される。体験談マイニングは注意点こそが素材の価値なので（#3203）、
これでは割に合わない。

そこで「**良い点だけでなく、不満・注意点・難点にも必ず1行以上使うこと**」を明示した
`summary_url_balanced` を試すと、**balance 1.00（全コールが注意点を含む）**、
注意点の数は現行の 0.67 → 2.00 に増え、**score も現行 0.824 を上回る 0.912** になった。
本文長は 385 で現行 384 と変わらない（URL のぶん本文が削られてはいない）。

**出典 URL を足すことと素材の質は両立する。ただし指示が要る。**

### 計器の訂正

初回集計では `japanese` が 1.00 → 0.00 に落ちていたが、これは**計器の artifact**
だった。grounding redirect の URL は 1 本 300 字超あり、日本語文字の**比率**で
測ると薄まって「英語で返ってきた」と誤判定する。URL を除いてから測るよう
`bench_agy_model.score_text` を修正した。修正後は全 variant で 1.00。
上表は修正後の値。

## 実装前に片付けるべき問題

### A. 自社サイトが出典として返ってくる（最重要）

ASIN `B00000DMD2` の 1 コールで、返った URL のうち 4 本が自社だった:

```
https://navi.omcha.jp/products/b00000dmd2/          ← まさにその ASIN の自社記事
https://omcha.jp/learning-resources-popular-toys/   ← ×3
```

**自分の書いた記事を「購入者の口コミ」の出典として取り込む循環**になる。

さらに重要なのは、**これは今も起きている可能性が高い**ということ。現行レーンは
`source_url: ""` なので、自社記事を読んで要約していても**観測できない**。
URL を足すことは、traceability の獲得であると同時に **この循環を検出する手段**でもある。
（現行レーンが実際に自社記事を引いているかは未確認 — 検索が自社ページを返すことは
実測したが、それを要約に使ったかは URL が無いので分からない）

実装するなら `navi.omcha.jp` / `omcha.jp` / `home.omcha.jp` の除外は必須。

### B. URL の作文が 13〜19% ある

到達しなかった URL の内訳で目立つのは、**同一のハルシネーション URL**
`item.rakuten.co.jp/babybus/5014/` が 4 回。実在しそうな形をしているので、
**収集時に HTTP で叩いて 200 を確認しない限り信用できない。**

### C. 英語圏サイトが混ざる

`trustpilot.com` / `desertcart.com` / `mamamummymum.co.uk` / `babipur.co.uk` /
`walmart.com` など。日本語の口コミ素材としては価値が低い。

### D. 検索結果ページが混ざる

`search.rakuten.co.jp` / `search.kakaku.com`。既存の
`mine_experience.is_search_result_url`（#5490 案B）で落とせる。

### E. 難所商品で空応答が増える

`summary_url_balanced` は 9 コール中 1 コールが**リトライ 3 回とも空応答**で落ちた
（ASIN 4910762116、82.7s）。プロンプトが重くなるぶん #6580 の失敗モードに入りやすい。
n=9 なので率としては測れていない。

## 判断材料

**取れる**: 1 コールあたり 3.88 本、全コールで取得、87% が実在ページに解決、
15 ドメインに分散。しかも `summary_url_balanced` は**現行より素材の質が高い**。

**コスト**: レイテンシ 13.6s → 37.7s（2.8 倍）。20 ASIN で約 13 分、
ジョブ timeout 60 分には収まる。加えて URL 検証のための HTTP アクセスが
1 コールあたり約 4 本増える。

**実装に含める必要があるもの**: 自社ドメイン除外 / HTTP 200 検証 /
検索結果ページ除外 / 日本語サイト優先。URL を素通しで `source_url` に入れてはいけない。

## 測っていないもの

- **ローカル gemma への要約移譲**。当初構想の後半。gemma は K8 ワーカー側にしか
  無く omochairo 機からは叩けないので、依然として未検証。なお probe の結果、
  agy から抜粋は取れないと分かったので、**この構想は前提から作り直しが要る**。
- **現行レーンが実際に自社記事を引いているか。** URL が無いので遡って調べられない。
- `summary_url_balanced` の空応答率。n=9 では出せない。

## 再実行

```bash
python3 -m scripts.probe_agy_sources --trials 3 --products 3 --out /tmp/sources.json
python3 -m scripts.probe_agy_sources --report /tmp/sources.json
```

本文と URL の解決結果を記録に残すので、採点器を直したら `--report` で採り直さずに
採点し直せる。
