"""增量规划：源差异 → 受影响页 → work order。

为什么不让 LLM 自己判断改哪些页
------------------------------
「一次摄入改动 8–15 个页」是**结果**，不是方法。让模型每次读完全站再决定，
成本随语料线性增长，而且不可复现。这里用确定性依赖图算范围，LLM 只负责写内容。

Triage（吸收社区实践）
--------------------
新增文档先做处置判定，`No material` 是独占的：
  New         —— 有足够材料建新页
  Update      —— 命中已有页的别名/标题，并入
  No material —— 素材太薄，只记日志，不硬凑页面
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import contracts
from .state import SourceState


@dataclass
class Diff:
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)

    def as_dict(self) -> Dict:
        return {"added": self.added, "modified": self.modified,
                "deleted": self.deleted, "unchanged": len(self.unchanged)}


@dataclass
class Impact:
    page: str
    doc_id: str
    reason: str
    mode: str = "recompile"          # recompile | new | stale


def diff_sources(current: Dict[str, SourceState],
                 previous: Dict[str, SourceState]) -> Diff:
    d = Diff()
    for doc_id, cur in current.items():
        prev = previous.get(doc_id)
        if prev is None:
            d.added.append(doc_id)
        elif cur.sha256 != prev.sha256:
            d.modified.append(doc_id)
        else:
            d.unchanged.append(doc_id)
    d.deleted = sorted(set(previous) - set(current))
    return d


def changed_headings(cur: SourceState, prev: SourceState) -> List[str]:
    """返回内容有变化的规范化标题；新增/删除的章节也算。"""
    a, b = prev.heading_keys(), cur.heading_keys()
    out = []
    for key in set(a) | set(b):
        if a.get(key) != b.get(key):
            out.append(key)
    return sorted(out)


def resolve_doc_id(ref: str, titles: Dict[str, str]) -> str:
    return titles.get(contracts.norm_name(ref), ref)


def impact(diff: Diff, pages: List[Dict], titles: Dict[str, str],
           current: Dict[str, SourceState],
           previous: Dict[str, SourceState]) -> List[Impact]:
    """把「哪些文档变了」映射成「哪些页要重编」。"""
    out: List[Impact] = []
    hit: Set[Tuple[str, str]] = set()

    for doc_id in diff.modified + diff.deleted:
        changed = set()
        if doc_id in diff.modified:
            changed = set(changed_headings(current[doc_id], previous[doc_id]))
        for page in pages:
            for src in page.get("source_refs", []):
                if resolve_doc_id(src, titles) != doc_id:
                    continue
                if not changed or any(
                        contracts.norm_name(h) in changed for h in src_headings(page, src, titles, doc_id)):
                    if (page["slug"], doc_id) not in hit:
                        hit.add((page["slug"], doc_id))
                        out.append(Impact(
                            page=page["slug"], doc_id=doc_id,
                            reason=("源文档已删除" if doc_id in diff.deleted
                                    else ("章节变更：%s" % ", ".join(sorted(changed))[:80]
                                          if changed else "正文变更（标题未变）")),
                            mode="stale" if doc_id in diff.deleted else "recompile"))

    for doc_id in diff.added:
        out.extend(triage_new(doc_id, pages, titles, current[doc_id]))
    return out


def src_headings(page: Dict, src_ref: str, titles: Dict[str, str],
                 doc_id: str) -> List[str]:
    for s in page.get("sources", []):
        if resolve_doc_id(str(s.get("doc", "")), titles) == doc_id:
            return [str(c.get("heading", "")) for c in (s.get("cites") or [])]
    return []


def triage_new(doc_id: str, pages: List[Dict], titles: Dict[str, str],
               state: SourceState) -> List[Impact]:
    """新文档的三种处置。"""
    names = {contracts.norm_name(doc_id)}
    for h in state.headings:
        names.add(contracts.norm_name(h.get("norm", "")))

    hits = []
    for page in pages:
        page_names = {contracts.norm_name(page["slug"]), contracts.norm_name(page["title"])}
        for a in page.get("aliases", []):
            page_names.add(contracts.norm_name(a))
        # 用**包含**匹配而不是精确相等：文档名往往比页面标题长得多
        # （`Human-in-the-loop Agent 设计：…` vs 页面别名 `Human-in-the-Loop`），
        # 精确相等几乎命中不了，新文档会全部掉进候选池。
        matched = sorted({d for d in names for pnm in page_names
                          if len(d) >= 4 and len(pnm) >= 4 and (d in pnm or pnm in d)})
        if matched:
            hits.append((page["slug"], matched[:3]))

    if hits:
        return [Impact(page=slug, doc_id=doc_id,
                       reason="新文档命中已有页：%s" % ", ".join(o), mode="recompile")
                for slug, o in hits]
    return [Impact(page="(候选)", doc_id=doc_id,
                   reason="新文档无既有页命中，进入候选池", mode="new")]


def build_order(page_slug: str, impacts: List[Impact], page_md: Optional[str],
                sources: List[Dict], glossary_excerpt: str) -> Dict:
    """一份 work order：LLM 需要的全部输入 + 必须遵守的输出契约。"""
    return {
        "version": 1,
        "slug": page_slug,
        "mode": "recompile" if page_md else "new",
        "reasons": [i.reason for i in impacts],
        "existing_page": page_md or "",
        "sources": sources,
        "glossary": glossary_excerpt,
        "contract": {
            "sections": contracts.SECTIONS,
            "sections_required": True,
            "citation_format": "【[[文档标题#规范化小节标题]]】",
            "language": "zh",
            "min_cjk_ratio": 0.45,
            "max_page_chars": 3000,
            "frontmatter_required": list(contracts.REQUIRED_META),
            "rules": [
                "每条事实性断言必须带引用；不得写来源里没有的内容",
                "不同文档说法不一致时并列呈现，不得擅自裁决",
                "专有名词保留英文（AWS KMS / OpenBao / agentgateway / MCP / OWASP）",
                "禁用译法见 glossary.banned",
            ],
        },
    }


def write_orders(orders: List[Dict], out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for order in orders:
        path = os.path.join(out_dir, "%s.json" % order["slug"].replace("/", "__"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(order, f, ensure_ascii=False, indent=2)
        paths.append(path)
    return paths


def file_hash(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def build_inventory(collection: str, label: str, docs: List[SourceState]) -> Dict:
    """新 collection 的「页面清单」任务。

    增量流程只能重编**已有页**，所以一个还没有页的 collection 永远拿不到
    第一批页。这里产出一份盘点任务：列出所有源文档与标题，让 LLM 提出页面划分。
    """
    return {
        "version": 1,
        "slug": "_inventory",
        "mode": "inventory",
        "collection": collection,
        "label": label,
        "docs": [{
            "doc_id": d.doc_id,
            "rel_path": d.rel_path,
            "headings": [{"level": h.get("level", 2), "text": h.get("norm", "")}
                         for h in d.headings],
        } for d in docs],
        "contract": {
            "task": "为新 collection 提出页面清单",
            "rules": [
                "页面分三类：concept（机制/原则）、entity（可指称的具体对象）、topic（跨页综述）",
                "一个页面至少要有 2 篇来源文档支撑；只有 1 篇支撑的概念不要单独建页，并入父级",
                "单篇文档不必对应单页；先看主题，再看文档",
                "每页给出：slug、title、type、预计来源（doc_id + 小节标题）",
                "输出一个 JSON 数组，不要写页面正文",
            ],
        },
    }
