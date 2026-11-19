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
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import frontmatter

from . import contracts
from .state import SourceState

DEFINITION_RE = re.compile(
    r"(?i)\b(is|are|refers? to|means|describes|denotes|provides|represents)\b"
    r"|是|指|称为|叫做|用于|是一种|是一个|属于")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

EVIDENCE_POLICY = {
    "purpose": "写页时优先使用这些证据片段；不要通读整篇源文档。",
    "rules": [
        "每条事实性断言必须来自 evidence[].quote；不得引入片段之外的事实。",
        "引用仍写 【[[doc#heading]]】；heading 必须与 evidence 中的完全一致。",
        "证据不足时在 定义与边界 / 适用范围与局限 中说明，不要补写。",
        "需要更多上下文时，只读 evidence 指向的 doc#heading，不读整篇。",
    ],
}


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
    headings: List[str] = field(default_factory=list)  # 新文档命中的小节


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
    """返回内容有变化的规范化标题；新增/删除的章节也算。

    比较的是小节内容 hash（父节含子节），不是标题名。
    标题没变、正文变了时，只有真正改动的小节会出现在结果里。
    """
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
        # 页面 cites 里的 heading 和状态里的 norm 都可能带全角标点，
        # 两边统一走 norm_name 再比较，否则明明改了这一节却匹配不上。
        changed_norm = {contracts.norm_name(h) for h in changed}
        for page in pages:
            for src in page.get("source_refs", []):
                if resolve_doc_id(src, titles) != doc_id:
                    continue
                if not changed or any(
                        contracts.norm_name(h) in changed_norm
                        for h in src_headings(page, src, titles, doc_id)):
                    if (page["slug"], doc_id) not in hit:
                        hit.add((page["slug"], doc_id))
                        out.append(Impact(
                            page=page["slug"], doc_id=doc_id,
                            reason=("源文档已删除" if doc_id in diff.deleted
                                    else ("章节变更：%s" % ", ".join(sorted(changed))[:80]
                                          if changed else "文档级变更（小节内容未变）")),
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
    heading_norms = {contracts.norm_name(h.get("norm", "")) for h in state.headings}
    names |= heading_norms

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
            # 只把真正命中的小节带进 order / evidence，
            # 不要把新文档的「前 6 个标题」当成相关证据。
            heads = [h for h in matched if h in heading_norms]
            hits.append((page["slug"], matched[:3], heads or sorted(heading_norms)))

    if hits:
        return [Impact(page=slug, doc_id=doc_id,
                       reason="新文档命中已有页：%s" % ", ".join(o),
                       mode="recompile", headings=heads)
                for slug, o, heads in hits]
    return [Impact(page="(候选)", doc_id=doc_id,
                   reason="新文档无既有页命中，进入候选池", mode="new")]


def _section_map(body: str) -> List[Tuple[str, str, int]]:
    """把规范化正文按标题切段，返回 [(heading, text, start_offset)]。"""
    lines = body.split("\n")
    out: List[Tuple[str, str, int]] = []
    cur: Optional[Tuple[str, int]] = None
    buf: List[str] = []
    offset = 0
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            if cur:
                out.append((cur[0], "\n".join(buf), cur[1]))
            cur = (m.group(2).strip(), offset + len(line) + 1)
            buf = []
        elif cur:
            buf.append(line)
        offset += len(line) + 1
    if cur:
        out.append((cur[0], "\n".join(buf), cur[1]))
    return out


def _quote_candidates(text: str, limit: int = 2) -> List[str]:
    """从一个 section 里挑最像定义句的段落，压到 400 字内。"""
    scored: List[Tuple[int, str]] = []
    for para in re.split(r"\n\s*\n", text or ""):
        q = re.sub(r"\s+", " ", para).strip()
        if not q or q.startswith("```") or q.startswith("|"):
            continue
        q = q[:400]
        score = 2 if DEFINITION_RE.search(q) else 0
        score += 1 if 80 <= len(q) <= 400 else 0
        scored.append((score, q))
    scored.sort(key=lambda x: -x[0])
    return [q for _, q in scored[:limit]]


def attach_evidence(order: Dict, raw_dir: str, existing_page: str = "",
                    limit: int = 16) -> Dict:
    """给 update 的 recompile order 补 evidence packet。

    update 原本只有 doc/heading；这里从 `data/raw/<collection>/<doc_id>.md`
    读取对应小节，抽 1–2 句定义性段落。已有 evidence 的不覆盖。
    """
    if order.get("evidence"):
        return order
    cited: Dict[str, List[str]] = {}
    if existing_page:
        try:
            post = frontmatter.loads(existing_page)
            for s in (post.metadata.get("sources") or []):
                heads = [str(c.get("heading") or "") for c in (s.get("cites") or [])]
                for key in (contracts.norm_name(str(s.get("doc") or "")),
                            contracts.norm_name(str(s.get("doc_id") or ""))):
                    if key and heads:
                        cited.setdefault(key, []).extend(heads)
        except Exception:
            pass

    collection = os.path.basename(os.path.normpath(raw_dir))
    evidence: List[Dict] = []
    seen: Set[Tuple[str, str, str]] = set()
    for src in order.get("sources") or []:
        doc_id = str(src.get("doc_id") or "")
        path = os.path.join(raw_dir, doc_id + ".md")
        if not doc_id or not os.path.exists(path):
            continue
        try:
            raw = open(path, encoding="utf-8").read()
            post = frontmatter.loads(raw)
            title = str((post.metadata or {}).get("title") or doc_id)
            body = post.content or ""
        except Exception:
            title, body = doc_id, ""
            raw = ""
        heads: List[str] = []
        for key in (contracts.norm_name(title), contracts.norm_name(doc_id)):
            heads.extend(cited.get(key, []))
        if not heads:
            heads = [str(h.get("norm") or h.get("orig") or "")
                     for h in (src.get("headings") or [])][:6]
        sections = _section_map(body or raw)
        for head in heads:
            if not head:
                continue
            key = contracts.norm_name(head)
            for heading, text, start in sections:
                if contracts.norm_name(heading) != key:
                    continue
                for q in _quote_candidates(text, 2):
                    dedupe_key = (doc_id, heading, q)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    evidence.append({
                        "collection": collection,
                        "doc": title,
                        "doc_id": doc_id,
                        "heading": heading,
                        "heading_path": heading,
                        "quote": q,
                        "offset": body.find(q),
                        "definitional": bool(DEFINITION_RE.search(q)),
                        "in_code": False,
                        "in_table": False,
                    })
                    if len(evidence) >= limit:
                        break
                break
            if len(evidence) >= limit:
                break
        if len(evidence) >= limit:
            break
    if evidence:
        order["evidence"] = evidence[:limit]
        order["evidence_policy"] = EVIDENCE_POLICY
    return order


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
