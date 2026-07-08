"""JP 用語 (タグ・ブランド名) → 英語スラッグの生成・永続マッピング (#2817)。

背景: navi のタグ (2620種・87%が日本語) とブランド名の URL がすべて日本語のため、
共有 UX・OS 間 (NFC/NFD) の文字コード不一致・解析ツールでの文字化けリスクを抱える。
本モジュールは JP 文字列 → 安定した英語スラッグへの変換を担う。

優先順位 (品質重視):
    1. data/brand_taxonomy.yaml の aliases に ASCII 表記があればそれを採用
       (例: "レゴ" → aliases に "LEGO" あり → slug "lego"。ローマ字変換だと "rego" になり誤り)
    2. 既に ASCII の用語はそのまま slugify
    3. それ以外は pykakasi でローマ字化 (かな/漢字読み)

一度 data/term_slugs.yaml に永続化したスラッグは **再計算しない** (build 毎に変わると
公開済み URL とリダイレクトが壊れるため)。衝突時は連番サフィックスで一意化する。

Public API:
    TermSlugMap(path=...).get_or_create(term) -> str
    TermSlugMap(path=...).get(term) -> str | None
"""
from __future__ import annotations

import pathlib
import re
import unicodedata
from typing import Optional

import yaml

_DEFAULT_SLUGS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "term_slugs.yaml"
)
_DEFAULT_BRAND_TAXONOMY_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "brand_taxonomy.yaml"
)

_ASCII_RE = re.compile(r"^[\x20-\x7e]+$")
_PAREN_RE = re.compile(r"^(.*?)[\(（]([^\)）]+)[\)）]\s*$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _is_ascii(s: str) -> bool:
    return bool(s) and bool(_ASCII_RE.match(s))


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = _NON_SLUG_RE.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s


def _romanize(s: str) -> str:
    import pykakasi

    kks = pykakasi.kakasi()
    romaji = "".join(item["hepburn"] for item in kks.convert(s))
    return romaji


def _fold_simple(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).upper()
    return re.sub(r"[\s\(\)（）・\-_/、,\.]+", "", s)


def _load_brand_ascii_aliases(path: pathlib.Path | None = None) -> dict:
    """brand_taxonomy.yaml の canonical(JP) → 最良 ASCII alias の対応表を作る。

    aliases には canonical 自身の英語併記 (例: "タカラトミー(TAKARA TOMY)") と、
    無関係な傘下シリーズ名 (例: タカラトミーの "ゾイド"/"ZOIDS", "トミカ"/"TOMICA") が
    同じ配列に混在する。誤って傘下シリーズの ASCII 名を拾わないよう、**canonical 自身と
    対を成す括弧併記形式のみ**を信頼する (括弧内 JP 部分が canonical と一致するもの)。
    一致するペアが無ければ候補なし (= romanize フォールバックに委ねる。長さ等で
    傘下シリーズ名を推測で採用すると誤爆するため)。
    """
    path = path or _DEFAULT_BRAND_TAXONOMY_PATH
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for entry in data.get("brands", []) or []:
        canonical = entry.get("canonical")
        if not canonical:
            continue
        canonical_folded = _fold_simple(canonical)
        for alias in entry.get("aliases") or []:
            alias = str(alias).strip()
            m = _PAREN_RE.match(alias)
            if not m:
                continue
            jp_part, ascii_part = m.group(1).strip(), m.group(2).strip()
            if _is_ascii(ascii_part) and _fold_simple(jp_part) == canonical_folded:
                out[canonical] = ascii_part
                break
        else:
            if _is_ascii(canonical):
                out[canonical] = canonical
    return out


def _base_slug(term: str, brand_aliases: dict) -> str:
    ascii_alias = brand_aliases.get(term)
    if ascii_alias:
        return _slugify(ascii_alias)
    if _is_ascii(term):
        return _slugify(term)
    return _slugify(_romanize(term))


class TermSlugMap:
    """data/term_slugs.yaml (JP用語 → 英語スラッグ) の読み書きと安定割当を担う。"""

    def __init__(
        self,
        path: pathlib.Path | str | None = None,
        brand_taxonomy_path: pathlib.Path | str | None = None,
    ) -> None:
        self.path = pathlib.Path(path) if path else _DEFAULT_SLUGS_PATH
        self._data: dict[str, str] = self._load()
        self._reverse: dict[str, str] = {v: k for k, v in self._data.items()}
        self._brand_aliases = _load_brand_ascii_aliases(
            pathlib.Path(brand_taxonomy_path) if brand_taxonomy_path else None
        )

    def _load(self) -> dict:
        if self.path.exists():
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return loaded or {}
        return {}

    def save(self) -> None:
        ordered = {k: self._data[k] for k in sorted(self._data)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(ordered, allow_unicode=True, sort_keys=True, default_flow_style=False),
            encoding="utf-8",
        )

    def get(self, term: str) -> Optional[str]:
        return self._data.get(term)

    def get_or_create(self, term: str) -> str:
        existing = self._data.get(term)
        if existing:
            return existing
        slug = self._unique_slug(_base_slug(term, self._brand_aliases) or "term")
        self._data[term] = slug
        self._reverse[slug] = term
        return slug

    def _unique_slug(self, base: str) -> str:
        if base not in self._reverse:
            return base
        n = 2
        while f"{base}-{n}" in self._reverse:
            n += 1
        return f"{base}-{n}"


def _cli() -> None:
    import sys

    m = TermSlugMap()
    changed = False
    for arg in sys.argv[1:]:
        slug = m.get_or_create(arg)
        changed = True
        print(f"{arg}\t{slug}")
    if changed:
        m.save()


if __name__ == "__main__":
    _cli()
