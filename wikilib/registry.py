"""实体注册表：canonical 名称、类型、别名、缩写展开。

为什么需要它
------------
页面 frontmatter 里的 alias 只能回答「这个页叫什么」，回答不了：
  * 同一个名称是否已经被某个实体占用（消歧）
  * 缩写的全称是什么（VPC → Virtual Private Cloud）
  * 一个候选应该新建页、并入现有页，还是只加别名

注册表是抽取 pipeline 的判定依据，也是 `entities.yaml` 的唯一事实源。
它是**人可编辑**的 YAML；sqlite 之类的派生索引都可以重建。

条目状态
--------
  active    已采纳，可作为页面或别名
  merged    已并入别的 canonical（保留记录，便于回滚）
  rejected  明确不建页（噪音/过时/重复）
  deferred  证据不足，等待人工或下一轮
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import yaml

from . import lexicon as lexicon_mod

VALID_TYPES = ("entity", "concept", "topic")
VALID_STATUS = ("active", "merged", "rejected", "deferred")


def norm(text: str) -> str:
    return lexicon_mod.normalize_term(text)


def slugify(name: str, fallback_prefix: str = "entity") -> str:
    """英文名 → 小写连字符 slug；中文名用稳定短哈希兜底。"""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if s and len(s) >= 2:
        return s
    digest = hashlib.md5((name or "").encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (fallback_prefix, digest)


@dataclass
class RegEntry:
    canonical: str
    type: str = "entity"
    collection: str = ""
    collections: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    acronym_of: str = ""
    qualifier: str = ""
    status: str = "active"
    slug: str = ""
    evidence: List[Dict] = field(default_factory=list)
    decisions: List[Dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return norm(self.canonical)

    def all_forms(self) -> List[str]:
        out = [self.canonical, self.acronym_of] + list(self.aliases)
        return [x for x in dict.fromkeys(out) if x]

    def as_dict(self) -> Dict:
        d = {
            "canonical": self.canonical,
            "type": self.type,
            "collection": self.collection,
            "collections": list(self.collections or ([self.collection] if self.collection else [])),
            "aliases": list(self.aliases),
            "acronym_of": self.acronym_of,
            "qualifier": self.qualifier,
            "status": self.status,
            "slug": self.slug or slugify(self.canonical),
        }
        if self.evidence:
            d["evidence"] = self.evidence
        if self.decisions:
            d["decisions"] = self.decisions
        return d


@dataclass
class Registry:
    entries: Dict[str, RegEntry] = field(default_factory=dict)
    path: str = ""

    def find(self, surface: str) -> Optional[RegEntry]:
        key = norm(surface)
        if not key:
            return None
        if key in self.entries:
            return self.entries[key]
        for e in self.entries.values():
            if any(norm(f) == key for f in e.all_forms()):
                return e
        return None

    def _safe_alias(self, alias: str, target_key: str) -> bool:
        other = self.find(alias)
        return other is None or other.key == target_key

    def add(self, entry: RegEntry) -> RegEntry:
        existing = self.find(entry.canonical)
        if existing is not None and existing.key != entry.key:
            # 同义：新 canonical 只是既有条目的另一个写法 → 并入，不另建。
            # 只有双方都明确给出不同 qualifier 时才按同形异义拆开。
            if entry.qualifier and existing.qualifier and entry.qualifier != existing.qualifier:
                entry.canonical = "%s (%s)" % (entry.canonical, entry.qualifier)
            else:
                if (norm(entry.canonical) != existing.key
                        and entry.canonical not in existing.aliases
                        and self._safe_alias(entry.canonical, existing.key)):
                    existing.aliases.append(entry.canonical)
                for a in entry.aliases:
                    if (a and norm(a) != existing.key and a not in existing.aliases
                            and self._safe_alias(a, existing.key)):
                        existing.aliases.append(a)
                for ev in entry.evidence:
                    if ev not in existing.evidence:
                        existing.evidence.append(ev)
                for d in entry.decisions:
                    if d not in existing.decisions:
                        existing.decisions.append(d)
                for c in list(entry.collections) + ([entry.collection] if entry.collection else []):
                    if c and c not in existing.collections:
                        existing.collections.append(c)
                existing.acronym_of = existing.acronym_of or entry.acronym_of
                if existing.status != "active" and entry.status == "active":
                    existing.status = "active"
                return existing
        key = entry.key
        if key in self.entries:
            cur = self.entries[key]
            cur.type = entry.type or cur.type
            cur.collection = entry.collection or cur.collection
            for c in list(entry.collections) + ([entry.collection] if entry.collection else []):
                if c and c not in cur.collections:
                    cur.collections.append(c)
            cur.acronym_of = entry.acronym_of or cur.acronym_of
            cur.qualifier = entry.qualifier or cur.qualifier
            cur.status = entry.status or cur.status
            cur.slug = cur.slug or entry.slug
            for a in entry.aliases:
                if (a and norm(a) != cur.key and a not in cur.aliases
                        and self._safe_alias(a, cur.key)):
                    cur.aliases.append(a)
            for ev in entry.evidence:
                if ev not in cur.evidence:
                    cur.evidence.append(ev)
            for d in entry.decisions:
                if d not in cur.decisions:
                    cur.decisions.append(d)
            return cur
        entry.aliases = [a for a in entry.aliases if a and self._safe_alias(a, entry.key)]
        entry.slug = entry.slug or slugify(entry.canonical)
        self.entries[key] = entry
        return entry

    def conflicts(self) -> List[Dict]:
        """同一写法被多个 canonical 占用（或类型冲突）。"""
        seen: Dict[str, List[RegEntry]] = {}
        for e in self.entries.values():
            for f in e.all_forms():
                k = norm(f)
                if k:
                    seen.setdefault(k, []).append(e)
        out = []
        for k, entries in seen.items():
            uniq = {e.key: e for e in entries}
            if len(uniq) > 1:
                out.append({
                    "surface": entries[0].canonical,
                    "key": k,
                    "entries": [e.canonical for e in uniq.values()],
                    "types": sorted({e.type for e in uniq.values()}),
                })
        return out

    def active(self) -> List[RegEntry]:
        return sorted((e for e in self.entries.values() if e.status == "active"),
                      key=lambda e: e.key)


def load(path: str) -> Registry:
    reg = Registry(path=path)
    if not path or not os.path.exists(path):
        return reg
    try:
        raw = yaml.safe_load(open(path, encoding="utf-8")) or []
    except yaml.YAMLError:
        raw = []
    if not isinstance(raw, list):
        raw = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("canonical"):
            continue
        e = RegEntry(
            canonical=str(item["canonical"]),
            type=str(item.get("type") or "entity"),
            collection=str(item.get("collection") or ""),
            collections=[str(x) for x in (item.get("collections") or [])],
            aliases=[str(x) for x in (item.get("aliases") or [])],
            acronym_of=str(item.get("acronym_of") or ""),
            qualifier=str(item.get("qualifier") or ""),
            status=str(item.get("status") or "active"),
            slug=str(item.get("slug") or ""),
            evidence=list(item.get("evidence") or []),
            decisions=list(item.get("decisions") or []),
        )
        reg.entries[e.key] = e
    return reg


def _uniq(items: Iterable) -> List:
    seen, out = set(), []
    for x in items or []:
        key = json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def save(reg: Registry, path: Optional[str] = None) -> str:
    path = path or reg.path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    for e in reg.entries.values():
        e.decisions = _uniq(e.decisions)
        e.evidence = _uniq(e.evidence)
    data = [e.as_dict() for e in sorted(reg.entries.values(), key=lambda e: e.key)]
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=100)
    return path


def merge(reg: Registry, entry: RegEntry) -> RegEntry:
    return reg.add(entry)


def from_lexicon(lex: lexicon_mod.Lexicon, collection: str = "") -> Registry:
    """把词表里的专有名词/缩写导入注册表，作为已知实体种子。"""
    reg = Registry()
    for e in lex.all_entries():
        if e.kind not in ("proper", "acronym"):
            continue
        reg.add(RegEntry(
            canonical=e.canonical,
            type="entity",
            collection=collection,
            aliases=list(e.aliases),
            acronym_of=e.expansion if e.kind == "acronym" else "",
        ))
    return reg
