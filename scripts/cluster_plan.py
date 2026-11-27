"""まとめの予定表 (data/cluster_plan.yaml) を読む (navi-brain#218 C1〜C3)。

これから作る・記事を足し続けたいまとめページ (時期のページ・特集) ごとに、商品に当てる語
(match)・Amazon の検索に使う語 (search)・目標の記事数・公開の目標日を書いておき、

- C2: fetch_amazon.py が毎回の検索語の頭に、予定表の検索語を数語入れる (レーン "cluster")
- C3: candidate_priority.py が、予定表に当たる商品の点を上げる (記事を書くレーンが先に選ぶ)

ために使う。外部 API は呼ばない。予定表が無い・壊れている・期限切れのときは空を返し、
呼び出し元は今までどおりに動く (記事の供給を止めない)。

予定表の 1 件:
    key: indoor-play-equipment       # 識別子 (英小文字・ハイフン)
    label: 体を動かす室内遊具          # 人が読む名前
    publish_by: 2027-05-01           # 公開の目標日 (省略可。近いほど優先)
    until: 2027-06-30                # この日を過ぎたら使わない (省略可)
    match: "滑り台|トランポリン"       # 商品名・タグに当てる正規表現
    exclude: "ミニカー|フィギュア"     # 当たっても外す正規表現 (省略可)
    search: [室内 滑り台, 子供 トランポリン]
    target_articles: 20              # 当たる記事がこの数に届いたら検索の枠を使わない
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re
import unicodedata
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "cluster_plan.yaml"
# 公開の目標日がこの日数以内なら「急ぎ」(点を 1 足す・検索の枠を先に使う)
URGENT_DAYS = 120


@dataclass(frozen=True)
class Cluster:
    key: str
    label: str
    match: re.Pattern
    exclude: re.Pattern | None
    search: tuple[str, ...]
    target_articles: int
    publish_by: dt.date | None = None
    until: dt.date | None = None

    def matches_text(self, text: str) -> bool:
        text = _norm(text)
        if not text or not self.match.search(text):
            return False
        return not (self.exclude and self.exclude.search(text))

    def days_left(self, today: dt.date) -> int | None:
        return (self.publish_by - today).days if self.publish_by else None

    def urgent(self, today: dt.date) -> bool:
        d = self.days_left(today)
        return d is not None and 0 <= d <= URGENT_DAYS

    def expired(self, today: dt.date) -> bool:
        return bool(self.until and today > self.until)


def _norm(text) -> str:
    # 全角英数・半角カナの揺れだけ畳む (大文字小文字は正規表現側で (?i) を使う)
    return unicodedata.normalize("NFKC", str(text or ""))


def _date(value) -> dt.date | None:
    # YAML の日時表記 (2027-05-01 00:00:00) は datetime で来る。date と引き算できないので日付だけにする
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def load(path=DEFAULT_PATH) -> list[Cluster]:
    """予定表を読む。読めない・形が違う行は警告して飛ばす (全体は止めない)。"""
    try:
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        logger.warning("cluster plan unreadable (%s): %s", path, e)
        return []
    rows = data.get("clusters") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        if rows is not None:
            logger.warning("cluster plan: clusters must be a list (%s)", path)
        return []
    out: list[Cluster] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        pattern = row.get("match")
        if not key or key in seen or not isinstance(pattern, str) or not pattern.strip():
            logger.warning("cluster plan: skipped a row without key/match or with a duplicate key: %r", key)
            continue
        try:
            match = re.compile(_norm(pattern))
            exclude = re.compile(_norm(row["exclude"])) if row.get("exclude") else None
        except re.error as e:
            logger.warning("cluster plan: bad regex in %s: %s", key, e)
            continue
        raw_search = row.get("search")
        if raw_search is not None and not isinstance(raw_search, list):
            # 文字列のまま書くと 1 文字ずつに分かれて検索の枠を無駄にするので、使わない
            logger.warning("cluster plan: search must be a list in %s", key)
            raw_search = []
        search = tuple(dict.fromkeys(str(s).strip() for s in (raw_search or []) if str(s).strip()))
        try:
            target = max(0, int(row.get("target_articles") or 0))
        except (TypeError, ValueError):
            target = 0
        seen.add(key)
        out.append(Cluster(key=key, label=str(row.get("label") or key), match=match, exclude=exclude,
                           search=search, target_articles=target,
                           publish_by=_date(row.get("publish_by")), until=_date(row.get("until"))))
    return out


def active(clusters, today: dt.date) -> list[Cluster]:
    """期限 (until) を過ぎていないもの。"""
    return [c for c in clusters if not c.expired(today)]


def item_text(item: dict) -> str:
    """候補 (amazon.json の item) の当てる文字列。商品名とブランド。"""
    return " ".join(str(item.get(k) or "") for k in ("title", "brand"))


def article_text(article: dict) -> str:
    """記事 JSON の当てる文字列。商品名とタグ。"""
    product = article.get("product") if isinstance(article.get("product"), dict) else {}
    tags = article.get("tags") if isinstance(article.get("tags"), list) else []
    return " ".join([str(product.get("name") or ""), *(str(t) for t in tags)])


def _urgency_key(c: Cluster, today: dt.date) -> tuple[int, int]:
    """急ぐ順の並べ方 (小さいほど先)。急ぐ予定 → 公開日が近い予定 → 公開日なし・過ぎた予定。"""
    d = c.days_left(today)
    return (0 if c.urgent(today) else 1, d if d is not None and d >= 0 else 10 ** 6)


def match_item(item: dict, clusters, today: dt.date) -> Cluster | None:
    """候補に当たる予定のうち、いちばん急ぐもの (並べ方は _urgency_key。同じなら予定表の順)。"""
    text = item_text(item if isinstance(item, dict) else {})
    hits = [c for c in active(clusters, today) if c.matches_text(text)]
    if not hits:
        return None
    return min(hits, key=lambda c: _urgency_key(c, today))


def count_articles(clusters, articles_dir="data/articles") -> dict[str, int]:
    """予定ごとの、当たる記事の数 (同じ ASIN は 1 本)。"""
    counts = {c.key: 0 for c in clusters}
    root = pathlib.Path(articles_dir)
    if not clusters or not root.is_dir():
        return counts
    seen: dict[str, set[str]] = {c.key: set() for c in clusters}
    for path in sorted(root.glob("*.json")):
        name = path.name
        if name.count(".") > 1:  # *.enrichment.json などの付属ファイル
            continue
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(article, dict):
            continue
        product = article.get("product") if isinstance(article.get("product"), dict) else {}
        asin = str(product.get("asin") or path.stem[-10:])
        text = article_text(article)
        for c in clusters:
            if asin not in seen[c.key] and c.matches_text(text):
                seen[c.key].add(asin)
    return {k: len(v) for k, v in seen.items()}


def search_keywords(clusters, slots: int, today: dt.date, counts: dict[str, int] | None = None,
                    rotation: int = 0) -> list[str]:
    """検索の枠に入れる語を slots 個まで返す (C2)。

    - 期限切れと、当たる記事が目標に届いた予定は使わない (counts が無ければ数で外さない)
    - 急ぐ予定 (公開日が URGENT_DAYS 以内) → 公開日が近い順 → 目標までの残りが多い順
    - 予定をまたいで 1 語ずつ交互に取る (1 つの予定が枠を全部使わない)
    - 同じ予定の中では rotation (呼び出し元が日付と時刻から作る) でずらし、毎回同じ語にしない
    """
    if slots <= 0:
        return []
    counts = counts or {}
    pool = []
    for c in active(clusters, today):
        if not c.search:
            continue
        have = counts.get(c.key)
        if have is not None and c.target_articles and have >= c.target_articles:
            continue
        left = (c.target_articles - have) if (have is not None and c.target_articles) else 0
        pool.append(((*_urgency_key(c, today), -left), c))
    pool.sort(key=lambda x: x[0])
    queues = []
    for _, c in pool:
        n = len(c.search)
        start = rotation % n
        queues.append([c.search[(start + i) % n] for i in range(n)])
    out: list[str] = []
    while len(out) < slots and any(queues):
        for q in queues:
            if q and len(out) < slots:
                kw = q.pop(0)
                if kw not in out:
                    out.append(kw)
    return out


def rotation_of(now: dt.datetime) -> int:
    """検索語をずらす数。01 は 1 日 2 回 (00:00・09:00 UTC) 走るので、回ごとに 1 つ進める。"""
    return now.date().toordinal() * 2 + (1 if now.hour >= 6 else 0)
