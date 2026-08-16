"""brand_normalizer.py

data/brand_taxonomy.yaml に従いブランド名表記ゆれを canonical 名に正規化する。

Public API:
    normalize(raw: str) -> NormalizedBrand
    get_taxonomy() -> Taxonomy

CLI:
    python scripts/brand_normalizer.py "アガツマ(AGATSUMA)"
    python scripts/brand_normalizer.py --dump-articles
"""
from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import yaml

_DEFAULT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "brand_taxonomy.yaml"
)


@dataclass(frozen=True)
class NormalizedBrand:
    canonical: str
    tier: str  # S/A/B/C/D
    region: str
    exclude_from_taxonomy: bool = False
    # noindex: ブランドとして登録 (正規化 / dedup / /brands/ ページ生成) はするが、
    # その /brands/ ページを noindex にする (検索インデックス非掲載)。
    # 中華系・無名級など、商品の正しい帰属はしたいが薄いハブページを Google に
    # 出したくないブランド向け (epic #2126 P4)。exclude_from_taxonomy とは独立。
    noindex: bool = False
    safety_default: tuple[str, ...] = ()
    matched_alias: Optional[str] = None
    match_type: str = "unknown"  # exact | fuzzy | unknown


def _fold(s: str) -> str:
    """比較用正規化: NFKC で全半角統一 → 大文字化 → 空白・記号除去."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.upper()
    s = re.sub(r"[\s\(\)（）・\-_/、,\.]+", "", s)
    return s


class Taxonomy:
    def __init__(self, path: pathlib.Path | None = None) -> None:
        path = path or _DEFAULT_PATH
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._brands: list[dict] = data.get("brands", []) or []
        self._exact: dict[str, dict] = {}
        # (folded_key, entry, original) for fuzzy contains-match
        self._fuzzy_keys: list[tuple[str, dict]] = []
        # folded(canonical) -> そのブランド名の別表記 (folded) 集合。canonical 自身を含む。
        self._name_variants: dict[str, frozenset[str]] = {}
        for entry in self._brands:
            canon = entry["canonical"]
            variants = {_fold(canon)}
            for v in entry.get("name_variants") or []:
                folded_v = _fold(str(v))
                if folded_v:
                    variants.add(folded_v)
            self._name_variants[_fold(canon)] = frozenset(variants)
            keys = [canon] + list(entry.get("aliases") or [])
            for k in keys:
                folded = _fold(k)
                if not folded:
                    continue
                self._exact.setdefault(folded, entry)
                # fuzzy は alias が 4文字以上のものだけ (誤マッチ防止)
                if len(folded) >= 4:
                    self._fuzzy_keys.append((folded, entry))

    def lookup(self, raw: str | None) -> NormalizedBrand:
        if not raw or not raw.strip():
            return self._unknown(raw or "")
        folded = _fold(raw)
        entry = self._exact.get(folded)
        if entry:
            return self._build(entry, raw, "exact")
        # fuzzy: alias の folded が入力に含まれる場合のみ採用
        for fkey, entry in self._fuzzy_keys:
            if fkey in folded:
                return self._build(entry, raw, "fuzzy")
        return self._unknown(raw)

    def _build(self, entry: dict, raw: str, match_type: str) -> NormalizedBrand:
        return NormalizedBrand(
            canonical=entry["canonical"],
            tier=str(entry.get("tier", "D")),
            region=str(entry.get("region", "unknown")),
            exclude_from_taxonomy=bool(entry.get("exclude_from_taxonomy", False)),
            noindex=bool(entry.get("noindex", False)),
            safety_default=tuple(entry.get("safety_default") or ()),
            matched_alias=raw,
            match_type=match_type,
        )

    def name_variants(self, canonical: str | None) -> frozenset[str]:
        """`canonical` と同じブランド名を指す別表記の folded 集合 (canonical 自身を含む)。

        `aliases` との違いが肝 (#5343 follow-up)。aliases は「この文字列を見たらこの
        ブランドに寄せる」ための正規化キーで、**傘下シリーズ名が意図的に混ざっている**
        (タカラトミーの "トミカ" / カワダの "ナノブロック" / ハズブロの "ナーフ" /
        ヨシリツの "ラキュー")。それらは商品の帰属を決めるには正しいが、
        「このブランドの人気シリーズ」としては**実在する情報**なので消してはいけない。

        一方で "BabyBus" に対する "ベビーバス"、"学研" に対する "Gakken" のような
        **同じ名前の別表記**は、シリーズ欄に出ると自己言及になるだけで情報量がゼロ。

        両者は文字列からは判別できない (ローマ字化しても "ベビーバス"→bebiibasu は
        "BabyBus" と一致せず、alias 照合では "トミカ" を巻き込む)。よって
        **データ側で明示する**: brand_taxonomy.yaml の任意キー `name_variants` に
        書かれたものだけを別表記として扱う。未指定のブランドは canonical のみが
        返るので、キーを足すまで挙動は変わらない。
        """
        return self._name_variants.get(_fold(canonical or ""), frozenset())

    def _unknown(self, raw: str) -> NormalizedBrand:
        return NormalizedBrand(
            canonical="ノーブランド",
            tier="D",
            region="unknown",
            exclude_from_taxonomy=True,
            matched_alias=raw,
            match_type="unknown",
        )


_DEFAULT: Optional[Taxonomy] = None


def get_taxonomy() -> Taxonomy:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Taxonomy()
    return _DEFAULT


def normalize(raw: str | None) -> NormalizedBrand:
    return get_taxonomy().lookup(raw)


def name_variants(canonical: str | None) -> frozenset[str]:
    return get_taxonomy().name_variants(canonical)


def _cli() -> None:
    import glob
    import os
    import sys

    tax = get_taxonomy()
    if "--dump-articles" in sys.argv:
        files = sorted(glob.glob("data/articles/*.json"))
        files = [
            f
            for f in files
            if not f.endswith((".enrichment.json", ".quality.json", ".seo.json"))
        ]
        agg: dict[tuple[str, str], int] = {}
        unknowns: list[tuple[str, str]] = []
        for f in files:
            d = json.load(open(f, encoding="utf-8"))
            raw = (d.get("product") or {}).get("brand", "") or ""
            r = tax.lookup(raw)
            agg[(r.canonical, r.tier)] = agg.get((r.canonical, r.tier), 0) + 1
            if r.match_type == "unknown" and raw:
                unknowns.append((os.path.basename(f), raw))
        print("=== Aggregated (canonical, tier) ===")
        for (c, t), n in sorted(agg.items(), key=lambda x: (-x[1], x[0][1], x[0][0])):
            print(f"  [{t}] {c}  x{n}")
        if unknowns:
            print("=== Unknowns (need alias update) ===")
            for f, raw in unknowns:
                print(f"  {f}: {raw!r}")
        return
    for arg in sys.argv[1:]:
        r = tax.lookup(arg)
        print(arg, "->", r)


if __name__ == "__main__":
    _cli()
