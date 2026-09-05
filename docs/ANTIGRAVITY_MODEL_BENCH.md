# agy (Antigravity CLI) のモデル選定 — 実測記録

`scripts/bench_agy_model.py` の実測に基づく、`mine_experience.gather_antigravity` の
モデル選定記録。**結論だけ変えたくなったら、まず bench を回し直すこと。**

## 対象の切り分け — agy を使っている処理は3つ、Gemini はそのうち1つ

| 呼び出し元 | workflow | モデル | 本件の対象 |
|---|---|---|---|
| `mine_experience.gather_antigravity` | `23-experience-mining.yml` (home-ops) | **agy CLI 既定 (= Gemini)** | ✅ **これ** |
| `draft_sns_reply.call_agy` | `29-sns-reply-inbox.yml` (home-ops) | `AGY_MODEL=claude-sonnet-4-6` | ❌ Claude なので対象外 |
| `generate_faq_seo` | — | agy は使わない (owner 確定・グラウンディング制約) | ❌ 対象外 |

## なぜピンするのか

`gather_antigravity` は `agy --print` を **`--model` 無し**で叩いていた。つまり
agy CLI の既定モデルに乗る。これが困るのは:

- 既定は agy のバージョン更新で**黙って動く**（実測時点 `agy 1.1.27`）
- `agy --output-format json` の応答に**モデル名が入らない**ので、
  本番のログからは何に乗っていたか**後から特定できない**

実測でも既定は 15 回中 2 回が空応答で、成功率は全 variant 中最下位だった。
「何に乗っているか分からないものが一番不安定」という状態なので、明示ピンする。

## 実測条件

```
agy 1.1.27 / omochairo 機 (Windows) / 2026-09-06
6 variant × 5 商品 (ブランド別) × 3 trial = 90 コール
商品: BabyBus / ラーニングリソーシズ / プラントイ / ブリオ / ボーネルンド
```

同一ブランドが per_asin に固まって並んでいるため、素直に先頭 5 件を取ると
全部 BabyBus のシールブックになる（初回はこれで回してしまい、破棄した）。
`load_products` は **1 ブランド 1 件**に絞っている。

## 採点

| 指標 | 重み | 中身 |
|---|---|---|
| `grounding` | 0.35 | 商品名・ブランドの語が本文に出るか（Web 検索が効いた証跡） |
| `balance` | 0.20 | 注意点・不満に触れているか |
| `no_refusal` | 0.20 | 「見つかりませんでした」等の逃げ文句が無いか |
| `format` | 0.15 | 箇条書き 3〜5 行（プロンプトの指示どおりか） |
| `japanese` | 0.10 | 日本語で返っているか |

失敗した trial は 0 点として平均に入れる（落ちる回数も品質のうち）。

### `balance` は後から足した — その経緯

当初は `grounding / no_refusal / format / japanese` の 4 指標だった。回してみると
**全 variant が 0.90〜0.97 に張り付いて選別できなかった**。物差しが分布の中央から
外れていて、何も選べていない状態。

本文を目視したところ、**3.1-pro 系は賞賛だけを並べて注意点を落とす**傾向が
はっきり出ていた。体験談マイニングは注意点・不満こそが記事素材の価値なので
（#3203 の凡庸化と直結する）、この差が指標に載っていないのは物差し側の欠陥。
そこで `balance` を足して再採点した。

**後付けの重み変更なので、変更前後の順位を両方残す。**

4 指標（balance 追加前）:

```
variant                    n    ok   score     sd
gemini-3.8-flash-low      15  1.00   0.967  0.047   ← 1位
gemini-3.1-pro-low        15  1.00   0.953  0.050
gemini-3.1-pro-high       15  1.00   0.947  0.050
gemini-3.8-flash-high     15  0.93   0.907  0.249
gemini-3.8-flash-medium   15  0.93   0.900  0.245
(default)                 15  0.87   0.818  0.325
```

5 指標（balance 追加後・確定版）:

```
variant                    n    ok   score     sd  grnd  gfull  cavs  praise   p50s   p95s  chars
gemini-3.8-flash-low      15  1.00   0.917  0.093  0.92   0.88  0.93    0.27   12.5   17.3    401  ← 1位
gemini-3.1-pro-low        15  1.00   0.853  0.112  0.88   0.85  0.47    0.53   19.1   23.2    309
gemini-3.8-flash-medium   15  0.93   0.851  0.240  0.91   0.89  0.93    0.29   17.4   23.7    431
gemini-3.8-flash-high     15  0.93   0.843  0.242  0.93   0.92  1.50    0.36   20.9   50.8    434
gemini-3.1-pro-high       15  1.00   0.807  0.100  0.87   0.83  0.27    0.73   21.2   23.5    304
(default)                 15  0.87   0.797  0.322  0.86   0.83  1.62    0.15   23.0   30.5    449
```

`praise` = 注意点に一切触れなかった応答の割合。

**どちらの物差しでも `gemini-3.8-flash-low` が 1 位。** 重みの後付けで順位を
作ったわけではない。

## 結論

**`gemini-3.8-flash-low` を採用。**

- 成功率 15/15（唯一 flash 系で無事故、かつ 3.1-pro と同率トップ）
- スコア 1 位（両ルーブリックとも）、分散も最小クラス（sd 0.093）
- **最速**: p50 12.5s / 平均 13.8s。既定（23.0s / 24.3s）の約半分
- 注意点に触れた率 73%（3.1-pro-high は 27% しかない）
- 失敗しやすい商品でも 8/8（既定は同じ商品で 2/8）

`3.1-pro` を採らない理由: **賞賛のみの要約になりやすい**（pro-high は 73% が
注意点ゼロ）。出力も短く（304 字 vs flash-low 401 字）、素材としての情報量が
少ない。推論の強いモデルが素材収集で強いとは限らない、という実測。

`3.8-flash-high` は grounding が最高（0.93）で注意点も多いが、p95 が 50.8s と
跳ねる（p50 の 2.4 倍）うえ空応答を 1 回出した。`low` との差はこのレーンの
要求に対して見合わない。

### レイテンシは制約としてバインドしていない

このレーンは 1 日 20 ASIN の cron で、コールあたり timeout 120s / ジョブ
timeout 60 分。20 × 23s ≈ 8 分なので、**既定のままでも時間には収まっていた**。
速さは決め手ではなく、あくまで同点時のタイブレーク。決め手は成功率と
`balance`。

## 空応答の切り分け — 商品差ではなくモデル差だった

本ベンチの失敗 4 件は**すべて同じ ASIN (4910762116 / BabyBus シールブック)**
に集中していた。この時点では「難しい商品なので全モデルが等しく落ちる」＝
商品差の可能性があり、成功率の差をモデルの差として読むのは危険だった。

そこでこの ASIN だけに絞って 6 variant × 5 trial を追加実測した。本ベンチの
3 trial と合わせた 8 回:

```
gemini-3.8-flash-low     8/8 = 1.00
gemini-3.1-pro-low       8/8 = 1.00
gemini-3.1-pro-high      8/8 = 1.00
gemini-3.8-flash-medium  6/8 = 0.75
gemini-3.8-flash-high    6/8 = 0.75
(default)                2/8 = 0.25   ← 既定だけ壊滅
```

**商品差ではなくモデル差**。難しい商品ほど差が開き、既定モデルはこの商品で
4 回に 3 回 空応答（`status: SUCCESS` かつ `response` が空文字）を返す。

失敗モードが厄介なのは、agy が **exit 0 かつ status SUCCESS のまま空文字を返す**
こと。`gather_antigravity` は空応答を warning + skip で握るので、**レーンは緑の
まま体験談だけが入って来ない**。既定モデルに乗せたままだと、この静かな欠落が
商品によって高頻度で起きる。ピンする理由はスコアより先にこれ。

## 測っていないもの

- **下流の gemma judge（`extract_snippets`）の歩留まり**。gemma は K8 ワーカー
  側にしか無く、omochairo 機からは叩けない。「agy の出力が最終的に何本の
  snippet になったか」は**このベンチでは測れていない**。本当に効いたかは
  本番の `data/raw/per_asin/**/experience.json` の件数で後追いすること。
- **コスト**。agy はユーザー quota 側で、コール単価の内訳が取れない。

## 再実行

```bash
python3 -m scripts.bench_agy_model --trials 3 --products 5 --out /tmp/bench.json
```

本文を記録に残しているので、採点器を直したときは**採り直さずに**採点し直せる:

```bash
python3 -m scripts.bench_agy_model --report /tmp/bench.json
```

新しいモデルが出たら `--variants` に足して回す。既定 (`--model` 無し) は
`""` で指定する。
