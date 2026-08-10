#!/usr/bin/env python3
"""probe_ubersuggest_products.py

Ubersuggest 需要語 (L1 通過分) が Amazon の商品として成立するかを **観測だけ**
する shadow レーン (#2686 PR-D)。

なぜ必要か:
  scripts/ingest_ubersuggest.py (PR-C) の語彙ゲート (data/demand_query_rules.yaml)
  だけでは商品抽出はできない。実測で L1 通過後も上位に非商品が残る
  (「知育 村」「みみっち」「すいちゃん みいつけた」「こども新聞」
  「たまごっち 種類」等)。これらは主題そのものが非商品というより、Amazon の
  商品として検索して初めて分かる種類の失敗 (キャラクター名単体・雑誌名・
  「種類」のような商品を指さない末尾語) なので、実際に SearchItems を叩いて
  判定する必要がある。

処理の流れ (語ごと):
  1. data/analytics/ubersuggest_demand.json の keywords を Volume 降順に
     --limit 件取る
  2. **raw_query** (空白を保持した元表記) で SearchItems を叩く。query は
     build_demand_keywords.normalize_key で重複排除のため空白を除去した
     キーなので検索語にしてはいけない (「保育園シール貼り」という一続きの
     文字列で検索すると「保育園 シール貼り」より一致率が落ちる)。
  3. 返った商品にジャンル判定 (scripts/genre_gate.classify_genre、再実装しない)
     をかけ、"pass" だけを genre_pass_hits として数える。"indeterminate"
     (fail-open) は生成パイプライン (fetch_amazon.py) では素通りさせるが、
     本レーンは「確認できたか」の観測なので indeterminate は数えない
     (fail-open にしない)。
  4. genre_pass_hits の商品タイトルとクエリ語の重なりを見て (下記
     compute_title_overlap)、verdict を確定する。

verdict の判定基準 (2026-08-10 設計、2026-08-10 owner レビューで partial/zero を
入れ替え・docstring 固定・unit test で担保):
  - hits == 0                        → non_product (no_hits)   Amazon に何も無い
  - genre_pass_hits == 0             → non_product (no_genre_pass)
      供給はあるがおもちゃ/ベビー領域ではない
  - coverage == 1.0 (クエリの全トークンが同一タイトル内に見つかる)
                                      → product (full_title_overlap)
  - coverage == 0.0 (どのタイトルにも1トークンも見つからない)
                                      → non_product (zero_title_overlap)
      ジャンルは通ったが検索語との関連が1つも確認できない = 返っているのは
      クエリと無関係な商品だけ、という強いシグナル。実例:「みみっち」で
      返る商品がすべて「たまごっち本体」を指すタイトルで、「みみっち」という
      トークンがどれにも現れない場合。
  - 0 < coverage < 1.0               → ambiguous (partial_title_overlap)
      一部のトークンだけ一致。初版 (2026-08-10) はここを non_product に固定
      していたが、owner レビューで「一部のトークンがタイトル表記と揺れる
      正当な商品語 (語順違い・別表記の複合語等) を誤って non_product に落とす
      おそれがある」との指摘を受けて変更した。coverage の根拠は「クエリを
      空白区切りにした表層トークン」であり、意味的な同義語・語順違い
      (例: 「オルゴールメリー」というクエリ語に対しタイトルが「メリー
      オルゴール」と逆順の複合語で書かれている場合) を検出できない。この
      検出限界がある以上、部分一致は「たまごっち 種類」のような真の非商品
      パターンと、表記揺れで一部だけ一致しなかった真の商品パターンの
      **両方を含みうる**。区別する根拠が無い状態で non_product に倒すと、
      表記揺れ側の実在商品を機械的に握り潰すことになり、これは L1 の
      store_navigational で「ボーネルンド おもちゃ」を誤除外していたのと
      同じ種類の事故になる。したがって部分一致は ambiguous にして owner の
      目視レビューに回す。
  - API 例外                          → ambiguous (api_error)
      判定材料が無いので不明。product にも non_product にも潰さない。
  - coverage == 1.0 だが genre_pass_hits/hits < WEAK_GENRE_EVIDENCE_RATIO
                                      → ambiguous (weak_genre_evidence)
      2026-08-10 #2686 案2 追加。coverage が満点でも、その一致が genre_pass
      した商品のごく一部でしか成立していなければジャンル的な裏付けが弱い
      (実測: 「パルクール」hits=10/genre_pass=3、「黒 ヒゲ」hits=10/
      genre_pass=4、「木戸木戸」hits=10/genre_pass=2。木戸木戸は
      「キドキド」の誤変換でボーネルンドの施設名、単体商品ではない)。
      ここでも non_product には落とさず、案2 のローカル LLM 判定に委ねる
      (judge_verdict の docstring 参照)。

  この非対称設計 (「ambiguous を product にも non_product にも潰さない」) は
  #4892 の L1 gate と同じ規律に従う。断定できない語は次の判断者 (人間) に
  渡す。verdict のうち non_product だけが「確認材料が十分にある」場合
  (hits=0 / genre_pass_hits=0 / coverage=0 の3パターン) に限定されている
  ことに注意 (部分一致は確認材料が不十分なので non_product ではない)。

制約 (scripts/probe_demand_supply.py の流儀を踏襲):
  - 1 keyword あたり SearchItems 1 回、1.1 秒間隔
  - API 失敗は keyword 単位で記録して次へ進む (1 語の失敗で全体を落とさない)
  - **data/raw/amazon.json を書かない** = Jules の生成プールに一切入らない
    = 記事は 1 本も作られない
  - --dry-run で API を呼ばず対象語の確認だけできる

レスポンス構造:
  SearchItems の実レスポンスは searchResult.items (probe_demand_supply.py の
  実測どおり)。browse_nodes の取り出しは fetch_amazon.extract_browse_nodes を
  そのまま再利用する (browseNodeInfo.browseNodes の ancestor チェーン走査を
  自前実装しない。2026-08-10 に searchResult.items の読み違いで 98/98 が
  0 件になった事故があるため、レスポンス構造は必ず既存実装から借りる)。
  resources も fetch_amazon.SEARCH_ITEM_RESOURCES (dry-run gate で実証済み)
  をそのまま使う。

タイトル照合の再設計 (2026-08-10 #2686 PR-E):
  #4893/#4894 で実施した workflow 50 の 200 語実査 (run 31377655321、API
  エラー 0) で、当初の「クエリを空白 split したトークンの部分文字列一致」
  による coverage が 1.0 / 0.5 / 0.0 の3値に張り付き (104/40/51件)、
  非商品として落ちた51語のうち37語は genre_pass_hits>=5 (Amazon がおもちゃを
  返している) という機能不全が実測された。原因は日本語に空白区切りが無い
  ことで、「プレミアムトミカ」(Volume 40,500) のような複合語が1トークンに
  なり、実タイトル「トミカプレミアム 05 …」と語順が逆なだけで一致しない
  誤判定が起きていた (「リカちゃん人形」も同様)。

  この PR では compute_title_overlap を形態素解析ベースに作り直した
  (janome 採用理由: 純 Python・辞書同梱・CI で C 拡張のビルド問題が出ない)。
  クエリ・タイトルの両方を content_words() で内容語 (名詞・動詞・形容詞・
  副詞。助詞・助動詞・記号・接続詞・フィラー・感動詞・連体詞・接頭詞、
  および単独の数字 (品詞細分類「数」) は除外、品詞名は unit test で固定) に
  分解し、**集合として** (語順を問わず) 一致数を数える。

  IPADIC の既知の限界: 「トミカ」のような片仮名の商品名・略称は辞書に
  無いことが多く、未知語処理で隣接する片仮名を丸ごと1トークンにまとめて
  しまう (「プレミアムトミカ」も「トミカプレミアム」もそれぞれ1トークン
  になり、両者は文字列として不一致のまま)。この丸呑みを検出する目印は
  janome の reading フィールドが '*' (未知語推定、辞書ヒットではない) に
  なること。_split_katakana_blob はこの丸呑みトークンの中から「reading が
  埋まっている (=辞書に実在する) 最長の部分文字列」をアンカーとして探し、
  前後の残り (辞書ヒットではないが非空) も別の内容語として切り出す。
  「プレミアムトミカ」→ アンカー「プレミアム」(辞書ヒット) + 残り「トミカ」
  → [プレミアム, トミカ]。「トミカプレミアム 05 …」も同じアンカーで
  [トミカ, プレミアム] に分かれ、集合が一致して coverage=1.0 になる。
  アンカーが見つからない場合は丸呑みトークンのまま1語として扱う (誤爆に
  よる過分割を避けるため、分割を諦める側に倒す)。

  judge_verdict の3値 (product/non_product/ambiguous) としきい値の考え方は
  #4893 の設計をそのまま維持する (触っていない)。供給ゲート・ジャンル
  ゲート (hits/genre_pass_hits) も本 PR では変更しない。

sample_titles の全件保存 (2026-08-10 owner レビューで items[:5] から変更):
  probe_keyword は SearchItems が返した hits 件 (items[:5] ではなく items
  全件、item_count 既定10件なので最大10件) の商品タイトルを sample_titles に
  保存する。当初は先頭5件だけを保存していたが、実データ (200語) の
  --recompute で「ウッディー」「玩具」が product → non_product に後退する
  事故が判明し、原因調査で「元の実行時に coverage=1.0 を成立させた一致
  タイトルが6〜10件目にあり、5件しか保存していなかったせいで再採点では
  見えなくなっていた」ことを特定した。これは --recompute だけでなく
  **後続 (#2686 案2: ローカル LLM 判定) でも同じ情報欠落を起こす**
  (LLM に渡せるタイトルが5件しか無ければ LLM も同じ誤判定をする)。
  したがって全件保存に変更し、情報欠落を再採点・LLM 判定の両方から
  取り除いた。フィールド名は sample_titles のまま据え置く (実データ
  data/analytics/ubersuggest_product_probe.json は5件保存の旧フォーマットで
  既にマージ済みだが、キー名を変えていないので recompute_verdicts はそのまま
  読める。旧フォーマットの語は引き続き5件分の情報でしか再採点できない
  ことに変わりはない)。

オフライン再採点 (--recompute):
  data/analytics/ubersuggest_product_probe.json には各語の sample_titles が
  既に保存されているので、API を1回も呼ばずに新しい coverage ロジックで
  採点し直せる。ただし同ファイルは上記の5件保存の旧フォーマットで生成
  されているため、再採点の coverage は実際 (item_count 件全体) より低めに
  出る可能性がある (一致機会がタイトルの数だけ減るため。実測: 200語中
  187語が5件・9語が0件)。確定は workflow 50 の再実行 (API 実行、今後は
  全件保存になる) で行うこと。recompute_verdicts の docstring も参照。

  後続 (#2686 案2: ローカル LLM 判定) で ambiguous と判定された語を再度
  API を叩かずに判定できるよう、sample_titles は probe_keyword の結果にも
  recompute の結果にも必ず残す。

使い方:
    python scripts/probe_ubersuggest_products.py --dry-run --limit 200
    python scripts/probe_ubersuggest_products.py --limit 200   # secrets が要る
    python scripts/probe_ubersuggest_products.py --recompute data/analytics/ubersuggest_product_probe.json
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from janome.tokenizer import Tokenizer

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import fetch_amazon as FA  # noqa: E402  browse_nodes 抽出・resources を再利用する
from genre_gate import classify_genre  # noqa: E402  再実装しない

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_ubersuggest_products")

DEFAULT_DEMAND_PATH = "data/analytics/ubersuggest_demand.json"
DEFAULT_OUT = "data/analytics/ubersuggest_product_probe.json"
DEFAULT_SEARCH_INDEX = "Toys"
DEFAULT_ITEM_COUNT = 10
DEFAULT_LIMIT = 200
SLEEP_SECONDS = 1.1

# fetch_amazon.SEARCH_ITEM_RESOURCES は 04-validate-article-pr.yml の dry-run
# gate (Issue #785) で実証済みの resource セット。itemInfo.title と
# browseNodeInfo.browseNodes(.ancestor) が既に含まれているので、新規に
# resource 名を作らずそのまま流用する (無効な resource は全 keyword 400 に
# なった実績があるため、実証済みのもの以外を自分で組み立てない)。
SEARCH_RESOURCES = FA.SEARCH_ITEM_RESOURCES

FULL_COVERAGE = 1.0
DEFAULT_RECOMPUTE_OUT = "data/analytics/ubersuggest_product_probe_recompute.json"

# ジャンル証拠の弱さのしきい値 (#2686 案2, 2026-08-10 追加)。
# coverage が 1.0 (全トークンがタイトルに一致) でも、その一致が genre_pass
# したごく一部の商品でしか成立していない場合は「product と断定できるほど
# ジャンル的な裏付けが無い」とみなす。実測 (workflow 50 run 31392220870、
# 200語) で以下の語が genre_pass_hits/hits < 0.6 のまま product と断定
# されていた:
#   パルクール     hits=10 genre_pass_hits=3
#   黒 ヒゲ        hits=10 genre_pass_hits=4
#   木戸木戸       hits=10 genre_pass_hits=2 (「キドキド」の誤変換、
#                  ボーネルンドの施設名でありおもちゃ単体商品ではない)
# 0.6 は上記実測に基づく暫定値であり、機械的に確定した最適値ではない
# (owner レビューでの再調整を妨げないよう定数として切り出す)。この
# しきい値を下回っても non_product には落とさない (案2の LLM 判定に送る、
# owner 方針: セッションコストを抑えたいので機械側で無理に減らさない)。
WEAK_GENRE_EVIDENCE_RATIO = 0.6

# 内容語として残す品詞 (名詞・動詞・形容詞・副詞)。それ以外 (助詞・助動詞・
# 記号・接続詞・フィラー・感動詞・連体詞・接頭詞) は coverage の分母から
# 除外する。品詞名は scripts/tests/test_probe_ubersuggest_products.py で
# 固定 (janome (IPADIC) の品詞体系: 品詞細分類1 が "数" の名詞 = 単独の数字
# も別途除外する)。
_CONTENT_POS_MAIN = {"名詞", "動詞", "形容詞", "副詞"}
# 片仮名の Unicode ブロック (U+30A0-U+30FF) には中黒「・」(U+30FB) や
# 濁点等の記号も含まれる。IPADIC の未知語処理は「同じ文字種の連続」を
# 丸呑みするため、「マグ・フォーマー」のような中黒入りの表記が中黒ごと
# 1トークンになってしまう。ここでは語を構成する片仮名だけ (ァ-ヶ・ー) に
# 絞り、中黒は _SEPARATOR_RE 側で別語の区切りとして扱う (実測: 「マグ
# フォーマー」で発覚、#2686 PR-E)。
_KATAKANA_RE = re.compile(r"^[ァ-ヺー-ヾ]+$")
_SEPARATOR_RE = re.compile(r"[・･/／]+")
_KATAKANA_SPLIT_MIN_LEN = 4  # これ未満は分割してもコストに見合わないので諦める

_tokenizer = Tokenizer()
_dictionary_hit_cache: dict[str, bool] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").casefold()


def _is_number_token(tok) -> bool:
    parts = tok.part_of_speech.split(",")
    return parts[0] == "名詞" and len(parts) > 1 and parts[1] == "数"


def _has_real_dictionary_reading(surface: str) -> bool:
    """surface 全体を1トークンとして解釈でき、かつ janome の reading が
    '*' でない (=IPADIC の辞書に実在する語であり、未知語処理による丸呑み
    ではない) ときに True を返す。_split_katakana_blob のアンカー探索で
    使う (モジュール docstring 「タイトル照合の再設計」参照)。"""
    if surface in _dictionary_hit_cache:
        return _dictionary_hit_cache[surface]
    toks = list(_tokenizer.tokenize(surface))
    result = len(toks) == 1 and toks[0].surface == surface and toks[0].reading != "*"
    _dictionary_hit_cache[surface] = result
    return result


def _split_katakana_blob(blob: str, _depth: int = 0) -> list[str]:
    """IPADIC が未知語処理で丸呑みした片仮名の連続 (「トミカプレミアム」等)
    を、辞書に実在する部分語 (reading が埋まっている) をアンカーに分割する。

    アンカーが見つからない場合は分割を諦めて blob をそのまま1語として返す
    (誤爆による過分割を避ける側に倒す)。再帰は深さ4まで (クエリ・タイトルの
    語は短いのでこれで十分)。"""
    if _depth > 4 or len(blob) < _KATAKANA_SPLIT_MIN_LEN or not _KATAKANA_RE.match(blob):
        return [blob] if blob else []

    n = len(blob)
    best: tuple[int, int, int] | None = None  # (length, start, end)
    for start in range(n):
        for end in range(n, start + 1, -1):  # 長い候補から (最長一致)
            piece = blob[start:end]
            if _has_real_dictionary_reading(piece):
                length = end - start
                if best is None or length > best[0]:
                    best = (length, start, end)
                break  # この start ではこれより長い piece は無い
    if best is None:
        return [blob]

    _, start, end = best
    pieces: list[str] = []
    if blob[:start]:
        pieces.extend(_split_katakana_blob(blob[:start], _depth + 1))
    pieces.append(blob[start:end])
    if blob[end:]:
        pieces.extend(_split_katakana_blob(blob[end:], _depth + 1))
    return pieces


def _expand_unknown_surface(surface: str) -> list[str]:
    """未知語処理で丸呑みされたトークンの surface を、まず中黒等の区切り
    記号 (_SEPARATOR_RE) で分割し、そのうえで各断片が長い片仮名の連続なら
    _split_katakana_blob でさらに分割する。"""
    pieces: list[str] = []
    for part in _SEPARATOR_RE.split(surface):
        if not part:
            continue
        if len(part) >= _KATAKANA_SPLIT_MIN_LEN and _KATAKANA_RE.match(part):
            pieces.extend(_split_katakana_blob(part))
        else:
            pieces.append(part)
    return pieces


def content_words(text: str) -> list[str]:
    """text を janome で分かち書きし、内容語 (名詞・動詞・形容詞・副詞。
    単独の数字を除く) だけを取り出す。活用のある語は base_form (辞書形) を
    使う。未知語処理で丸呑みされたトークン (片仮名の連続、中黒入りの表記
    「マグ・フォーマー」等) は _expand_unknown_surface でさらに分割を試みる
    (モジュール docstring 参照)。"""
    words: list[str] = []
    for tok in _tokenizer.tokenize(text or ""):
        pos_main = tok.part_of_speech.split(",")[0]
        if pos_main not in _CONTENT_POS_MAIN:
            continue
        if _is_number_token(tok):
            continue
        surface = tok.surface
        if not surface or not surface.strip():
            continue
        if tok.reading == "*":
            words.extend(_expand_unknown_surface(surface))
            continue
        base = tok.base_form if tok.base_form and tok.base_form != "*" else surface
        words.append(base)
    return [w for w in words if w and w.strip()]


def extract_items(response: Any) -> list[dict[str, Any]]:
    """SearchItems レスポンスから [{asin, title, browse_nodes}] を返す。

    構造判断は probe_demand_supply.extract_asins と同じ (searchResult.items、
    無ければトップレベル items にフォールバック)。browse_nodes は
    fetch_amazon.extract_browse_nodes をそのまま呼ぶ (自前実装しない)。
    形が違う/空のときは空リストを返す (例外にしない)。
    """
    if not isinstance(response, dict):
        return []
    items = response.get("searchResult", {})
    items = items.get("items") if isinstance(items, dict) else None
    if not isinstance(items, list):
        # 念のためトップレベルも見る (クライアント側で平坦化された場合)
        items = response.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        asin = it.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        title = FA._safe_get(it, "itemInfo", "title", "displayValue") or ""
        browse_nodes = FA.extract_browse_nodes(it)
        out.append({"asin": asin, "title": title, "browse_nodes": browse_nodes})
    return out


def compute_title_overlap(raw_query: str, titles: list[str]) -> float:
    """raw_query の内容語 (content_words、語順不問) が titles (genre_pass_hits
    のタイトルのみ) のうちどれか1つにどれだけ含まれるかを 0.0〜1.0 で返す。

    2026-08-10 #2686 PR-E で空白区切りの部分文字列一致から形態素解析ベースの
    集合一致に作り直した (モジュール docstring 「タイトル照合の再設計」
    参照)。titles が空 (genre_pass_hits=0)、または raw_query に内容語が
    無ければ 0.0。judge_verdict のしきい値と対で使うこと。
    """
    query_words = [_normalize_text(w) for w in content_words(raw_query)]
    query_words = [w for w in query_words if w]
    if not query_words or not titles:
        return 0.0
    best = 0
    for title in titles:
        title_word_set = {_normalize_text(w) for w in content_words(title)}
        matched = sum(1 for w in query_words if w in title_word_set)
        if matched > best:
            best = matched
        if best == len(query_words):
            break
    return best / len(query_words)


def judge_verdict(hits: int, genre_pass_hits: int, coverage: float) -> tuple[str, str]:
    """(verdict, reason) を返す。しきい値の根拠はモジュール docstring 参照。

    2026-08-10 owner レビューで partial/zero の割り当てを入れ替えた:
      - coverage == 0.0 (どのタイトルにも1トークンも無い) → non_product
        (無関係な商品しか返っていない、という強いシグナル)
      - 0 < coverage < 1.0 (一部だけ一致) → ambiguous
        (表記揺れ・語順違いで一致しなかった実在商品を巻き込みうるため、
        断定せず人間のレビューに回す)

    2026-08-10 #2686 案2 追加: coverage == 1.0 でも genre_pass_hits/hits が
    WEAK_GENRE_EVIDENCE_RATIO 未満なら product と断定しない (「パルクール」
    「黒 ヒゲ」「木戸木戸」の実測、定数の docstring 参照)。ここでも
    non_product には落とさず ambiguous (weak_genre_evidence) にして
    案2 のローカル LLM 判定に委ねる (機械側でこれ以上削り込まない、
    owner 方針)。
    """
    if hits == 0:
        return "non_product", "no_hits"
    if genre_pass_hits == 0:
        return "non_product", "no_genre_pass"
    if coverage >= FULL_COVERAGE:
        if (genre_pass_hits / hits) < WEAK_GENRE_EVIDENCE_RATIO:
            return "ambiguous", "weak_genre_evidence"
        return "product", "full_title_overlap"
    if coverage <= 0.0:
        return "non_product", "zero_title_overlap"
    return "ambiguous", "partial_title_overlap"


def probe_keyword(api, query: str, raw_query: str, volume: float, sites: list[str],
                  search_index: str, item_count: int) -> dict[str, Any]:
    """1 語を検索して verdict を確定する。API 例外は error として畳んで返す
    (verdict は ambiguous。判定材料が無いので product/non_product どちらにも
    倒さない)。
    """
    try:
        res = api.search_items(keywords=raw_query, search_index=search_index,
                               item_count=item_count, item_page=1,
                               resources=SEARCH_RESOURCES)
    except Exception as e:  # API 側の例外型に依存しない (1 語の失敗で全体を止めない)
        return {
            "query": query, "raw_query": raw_query, "volume": volume, "sites": sites,
            "error": f"{type(e).__name__}: {e}",
            "hits": 0, "genre_pass_hits": 0, "title_overlap": 0.0,
            "verdict": "ambiguous", "verdict_reason": "api_error",
            "sample_titles": [],
        }

    items = extract_items(res)
    hits = len(items)
    passing = [it for it in items if classify_genre(it["browse_nodes"], it["asin"])[0] == "pass"]
    genre_pass_hits = len(passing)
    coverage = compute_title_overlap(raw_query, [it["title"] for it in passing])
    verdict, reason = judge_verdict(hits, genre_pass_hits, coverage)

    return {
        "query": query, "raw_query": raw_query, "volume": volume, "sites": sites,
        "error": None,
        "hits": hits, "genre_pass_hits": genre_pass_hits,
        "title_overlap": round(coverage, 3),
        "verdict": verdict, "verdict_reason": reason,
        # 2026-08-10 #2686 PR-E owner レビューで items[:5] (先頭5件のみ) から
        # 全件保存に変更した。5件しか保存しないと --recompute (このモジュール)
        # だけでなく後続の案2 (ambiguous のローカル LLM 判定) でも同じ情報
        # 欠落が起きる (6〜10件目にしか無い一致タイトルを LLM も見られない)。
        # item_count は既定10件なので最大10件になる。フィールド名は
        # sample_titles のまま据え置く (実データ data/analytics/
        # ubersuggest_product_probe.json は5件保存の旧フォーマットで既に
        # マージ済みだが、recompute_verdicts はキー名を変えていないので
        # そのまま読める)。
        "sample_titles": [it["title"] for it in items],
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    product = [r for r in results if r["verdict"] == "product"]
    non_product = [r for r in results if r["verdict"] == "non_product"]
    ambiguous = [r for r in results if r["verdict"] == "ambiguous"]
    errors = [r for r in results if r["error"]]
    return {
        "keywords_probed": len(results),
        "product": len(product),
        "non_product": len(non_product),
        "ambiguous": len(ambiguous),
        "error_count": len(errors),
        "product_volume_sum": sum(r["volume"] or 0 for r in product),
    }


def _tally_verdicts(results: list[dict[str, Any]], key: str) -> dict[str, int]:
    tally: dict[str, int] = {"product": 0, "non_product": 0, "ambiguous": 0}
    for r in results:
        v = r.get(key)
        if v in tally:
            tally[v] += 1
    return tally


def recompute_verdicts(probe_path: pathlib.Path) -> dict[str, Any]:
    """既存の実査 JSON (probe_ubersuggest_products.py --limit N の出力) を
    API を一切呼ばずに新しい compute_title_overlap で再採点する
    (#2686 PR-E)。

    **制約 (必ず読むこと)**: sample_titles は probe_keyword 実行時の
    item_count 件 (既定10件) のヒットのうち items[:5] (先頭5件、
    genre_pass_hits で絞り込む前) しか保存されていない (実測: 実データ
    200 語中187語が5件・9語が0件)。したがってここで出る coverage は
    実際に item_count 件全体・genre_pass_hits 絞り込み後で再検索した
    場合より **低めに出る可能性がある** (一致機会がタイトルの数だけ減る
    ため)。hits/genre_pass_hits 自体は再検索していないので元の値を
    そのまま流用する (供給・ジャンルゲートはこの PR では変更していない)。
    これは「改善の方向と規模」を確認するための暫定値であり、確定は
    workflow 50 の再実行 (API 実行) で行うこと。

    verdict_reason が api_error (判定材料が無い) の語は再採点せず、
    verdict_before をそのまま verdict_after にも入れる (hits=0 のまま
    judge_verdict に通すと no_hits に誤って倒れてしまうため)。

    後続 (#2686 案2: ローカル LLM 判定) で ambiguous の語を再度 API を
    叩かずに判定できるよう、sample_titles は結果にそのまま残す。
    """
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    old_results = payload.get("results") or []

    new_results: list[dict[str, Any]] = []
    for r in old_results:
        verdict_before = r.get("verdict")
        reason_before = r.get("verdict_reason")
        overlap_before = r.get("title_overlap")
        sample_titles = r.get("sample_titles") or []
        hits = r.get("hits") or 0
        genre_pass_hits = r.get("genre_pass_hits") or 0

        if r.get("error") or reason_before == "api_error":
            verdict_after, reason_after, overlap_after = verdict_before, reason_before, overlap_before
        else:
            overlap_after = round(compute_title_overlap(r.get("raw_query", ""), sample_titles), 3)
            verdict_after, reason_after = judge_verdict(hits, genre_pass_hits, overlap_after)

        new_results.append({
            "query": r.get("query"), "raw_query": r.get("raw_query"), "volume": r.get("volume"),
            "sites": r.get("sites"),
            "hits": hits, "genre_pass_hits": genre_pass_hits,
            "sample_titles": sample_titles,
            "verdict_before": verdict_before, "verdict_reason_before": reason_before,
            "title_overlap_before": overlap_before,
            "verdict_after": verdict_after, "verdict_reason_after": reason_after,
            "title_overlap_after": overlap_after,
            "changed": verdict_before != verdict_after,
        })

    changed = [r for r in new_results if r["changed"]]
    return {
        "generated_at": _now_iso(),
        "source": str(probe_path),
        "note": ("sample_titles は先頭5件のみ保存されているため、この再採点は"
                 "実測より低めに出る可能性がある暫定値。確定は workflow 50 の"
                 "API 再実行で行うこと (recompute_verdicts の docstring参照)。"),
        "summary_before": _tally_verdicts(old_results, "verdict"),
        "summary_after": _tally_verdicts(new_results, "verdict_after"),
        "changed_count": len(changed),
        "results": new_results,
    }


def load_targets(demand_path: pathlib.Path, limit: int) -> list[dict[str, Any]]:
    payload = json.loads(demand_path.read_text(encoding="utf-8"))
    entries = payload.get("keywords") or []
    entries = sorted(entries, key=lambda e: -(e.get("volume") or 0))
    if limit > 0:
        entries = entries[:limit]
    return entries


def run(demand_path: pathlib.Path, out_path: pathlib.Path, limit: int, search_index: str,
        item_count: int, dry_run: bool, api=None, sleeper=time.sleep) -> dict[str, Any]:
    entries = load_targets(demand_path, limit)
    params = {"limit": limit, "search_index": search_index, "item_count": item_count}

    if dry_run:
        logger.info("[dry-run] API を呼ばずに終了する。対象語 %d 件", len(entries))
        for e in entries[:20]:
            logger.info("  %-24s volume=%s sites=%s", e["query"][:24], e.get("volume"),
                        ",".join(e.get("sites") or []))
        return {
            "generated_at": _now_iso(),
            "params": params,
            "summary": {"keywords_probed": 0},
            "results": [],
            "targets": [
                {"query": e["query"], "raw_query": e["raw_query"], "volume": e.get("volume"),
                 "sites": e.get("sites") or []}
                for e in entries
            ],
        }

    if api is None:
        from creators_api_client import CreatorsAPIClient  # 遅延 import (dry-run では不要)
        api = CreatorsAPIClient()

    results: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        r = probe_keyword(api, e["query"], e["raw_query"], e.get("volume") or 0,
                          e.get("sites") or [], search_index, item_count)
        results.append(r)
        logger.info("  [%3d/%3d] %-24s hits=%2d genre_pass=%2d overlap=%.2f verdict=%-11s %s",
                    i + 1, len(entries), e["query"][:24], r["hits"], r["genre_pass_hits"],
                    r["title_overlap"], r["verdict"], r["error"] or "")
        if i + 1 < len(entries):
            sleeper(SLEEP_SECONDS)

    summary = summarize(results)
    report = {"generated_at": _now_iso(), "params": params, "summary": summary, "results": results}

    logger.info("product %d 語 (volume合計 %d) / non_product %d 語 / ambiguous %d 語 / "
                "エラー %d 語",
                summary["product"], summary["product_volume_sum"], summary["non_product"],
                summary["ambiguous"], summary["error_count"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out_path)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ubersuggest 需要語の Amazon 実査 (観測のみ, #2686 PR-D/PR-E)")
    ap.add_argument("--demand", default=DEFAULT_DEMAND_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Volume上位N語 (0=全件)")
    ap.add_argument("--search-index", default=DEFAULT_SEARCH_INDEX)
    ap.add_argument("--item-count", type=int, default=DEFAULT_ITEM_COUNT)
    ap.add_argument("--dry-run", action="store_true", help="API を一切呼ばない")
    ap.add_argument("--recompute", metavar="PROBE_JSON", default=None,
                     help="既存の実査 JSON を API を呼ばず新しいタイトル照合ロジックで"
                          "再採点する (#2686 PR-E)。指定時は他の実査系引数は無視される")
    ap.add_argument("--recompute-out", default=DEFAULT_RECOMPUTE_OUT)
    args = ap.parse_args()

    if args.recompute:
        report = recompute_verdicts(pathlib.Path(args.recompute))
        out_path = pathlib.Path(args.recompute_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("recompute: before=%s after=%s changed=%d wrote %s",
                    report["summary_before"], report["summary_after"],
                    report["changed_count"], out_path)
        return 0

    run(pathlib.Path(args.demand), pathlib.Path(args.out), args.limit, args.search_index,
        args.item_count, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
