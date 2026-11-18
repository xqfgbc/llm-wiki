"""实体/概念抽取 pipeline（P1）。

四个阶段
--------
S1 候选生成（纯 Python）   从正文、标题、词表种子里找候选，带证据片段。
S2 确定性门禁              覆盖检查、频次阈值、缩写展开、同名/上下位冲突。
S3 对抗复核（LLM）         产出 proposer / challenger 两份**互相隔离**的工单。
S4 注册表合并              把裁决写回 entities.yaml，产出建页 order 与人审队列。

设计原则
--------
* 确定性规则决定「能不能进复核」，LLM 只决定「该不该建页、建在哪」。
* Challenger 的工单里**没有** proposer 的置信度和理由，避免锚定。
* 任何 AI 无法收敛的候选都进 `_human_queue`，不会被静默丢弃。
* 源码目录只读；所有产物写在 data/orders 与 data/review。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import contracts
from . import lexicon as lexicon_mod
from . import normalize as norm_mod
from . import registry as registry_mod

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,6}\b")
LEVEL_RE = re.compile(r"\bL[0-9]\b")
CAP_SEQ_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[ -][A-Z][A-Za-z0-9]*){1,3}\b")
CAMEL_RE = re.compile(r"\b(?:[a-z]{2,}[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")
HYPHEN_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-/&\. ]{1,50}")

DEFINITION_RE = re.compile(
    r"(?i)\b(is|are|refers? to|means|describes|denotes|provides|represents)\b"
    r"|是|指|称为|叫做|用于|是一种|是一个|属于")

# 标题式大写短语里常见的功能词：出现在开头/结尾就判为句子残片，不是实体
STOP_TITLE = {
    "The", "A", "An", "This", "That", "These", "Those", "It", "We", "You",
    "Our", "Your", "Their", "His", "Her", "In", "On", "At", "By", "For",
    "With", "From", "To", "Of", "As", "Is", "Are", "Was", "Were", "Be",
    "Been", "Has", "Have", "Had", "Not", "No", "If", "When", "While",
    "After", "Before", "During", "Between", "Among", "And", "Or", "But",
    "So", "Such", "Which", "Who", "What", "How", "Why", "Where", "All",
    "Any", "Each", "Every", "Some", "Many", "Most", "More", "Other",
    "Another", "One", "Two", "Three", "New", "Old", "First", "Last",
    "Next", "Previous", "There", "Here", "Then", "Than", "Also", "Only",
    "Just", "Very", "Can", "Could", "May", "Might", "Must", "Should",
    "Would", "Will", "Shall", "Do", "Does", "Did", "Done", "Use", "Used",
    "Using", "See", "Let", "Get", "Got", "Make", "Made", "Take", "Taken",
}
STOP_UPPER = {
    "THE", "AND", "FOR", "WITH", "NOT", "YOU", "ALL", "ANY", "CAN", "MAY",
    "BUT", "OUR", "ITS", "HIS", "HER", "THEIR", "THIS", "THAT", "THESE",
    "THOSE", "FROM", "INTO", "OVER", "UNDER", "WHEN", "WHAT", "HOW", "WHY",
    "WHO", "USE", "USED", "USING", "ARE", "WAS", "WERE", "HAS", "HAVE",
    "HAD", "WILL", "WOULD", "SHOULD", "COULD", "MUST", "NEW", "OLD", "ONE",
    "TWO", "THREE", "HTTP", "HTTPS", "TCP", "UDP", "TLS", "SSL", "CPU",
    "RAM", "API", "URL", "URI", "SDK", "CLI", "JSON", "YAML", "XML", "HTML",
    "SQL", "RPC", "GRPC", "OK", "NO", "YES", "IT", "IS", "TO", "OF", "IN",
    "ON", "AT", "AS", "BY", "OR", "IF", "DO", "BE", "ID", "UI", "OS",
}
BAD_SURFACE_RE = re.compile(
    r"^(?:https?://|www\.)|\.(?:md|py|js|ts|json|yaml|yml|png|jpg|pdf)$|"
    r"^[0-9a-f]{8,}$|^[A-Za-z]$")


@dataclass
class Evidence:
    doc_id: str
    doc_title: str
    heading: str
    heading_path: str
    quote: str
    offset: int
    collection: str = ""
    definitional: bool = False
    in_code: bool = False
    in_table: bool = False

    def as_dict(self) -> Dict:
        return {
            "collection": self.collection,
            "doc": self.doc_title,
            "doc_id": self.doc_id,
            "heading": self.heading,
            "heading_path": self.heading_path,
            "quote": self.quote,
            "offset": self.offset,
            "definitional": self.definitional,
            "in_code": self.in_code,
            "in_table": self.in_table,
        }


@dataclass
class Candidate:
    key: str
    surface: str
    variants: Counter = field(default_factory=Counter)
    mentions: int = 0
    docs: Set[str] = field(default_factory=set)
    collections: Set[str] = field(default_factory=set)
    headings: Set[str] = field(default_factory=set)
    evidence: List[Evidence] = field(default_factory=list)
    in_heading: bool = False
    code_mentions: int = 0
    table_mentions: int = 0
    definitional: int = 0
    score: float = 0.0
    gate: str = "needs_review"      # covered | observation | needs_review
    gate_reason: str = ""
    existing_page: str = ""
    lexicon_entry: Optional[Dict] = None
    proposed: Dict = field(default_factory=dict)
    candidate_id: str = ""

    def as_dict(self, include_evidence: bool = True) -> Dict:
        d = {
            "candidate_id": self.candidate_id,
            "surface": self.surface,
            "key": self.key,
            "variants": dict(self.variants),
            "mentions": self.mentions,
            "doc_coverage": len(self.docs),
            "docs": sorted(self.docs),
            "collections": sorted(self.collections),
            "headings": sorted(self.headings)[:8],
            "in_heading": self.in_heading,
            "definitional": self.definitional,
            "code_mentions": self.code_mentions,
            "table_mentions": self.table_mentions,
            "score": round(self.score, 3),
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "existing_page": self.existing_page,
            "proposed": self.proposed,
        }
        if self.lexicon_entry:
            d["lexicon"] = self.lexicon_entry
        if include_evidence:
            d["evidence"] = [e.as_dict() for e in self.evidence]
        return d


# ---------------------------------------------------------------- 结构扫描
def _scan_structure(body: str):
    """返回 (heads, code_intervals, table_intervals)。

    heads: [(start, level, text, path)]
    """
    lines = body.split("\n")
    offset = 0
    in_fence = False
    fence_start = 0
    heads: List[Tuple[int, int, str]] = []
    code: List[Tuple[int, int]] = []
    table: List[Tuple[int, int]] = []

    for line in lines:
        start, end = offset, offset + len(line)
        if FENCE_RE.match(line):
            if not in_fence:
                fence_start = start
                in_fence = True
            else:
                code.append((fence_start, end))
                in_fence = False
        elif not in_fence:
            m = HEADING_RE.match(line)
            if m:
                heads.append((start, len(m.group(1)), m.group(2).strip()))
            if line.lstrip().startswith("|"):
                table.append((start, end))
        offset = end + 1
    if in_fence:
        code.append((fence_start, offset))

    with_path = []
    for i, (start, level, text) in enumerate(heads):
        chain = []
        lv = level
        for j in range(i - 1, -1, -1):
            if heads[j][1] < lv:
                chain.append(heads[j][2])
                lv = heads[j][1]
        path = " > ".join(list(reversed(chain)) + [text])
        with_path.append((start, level, text, path))
    return with_path, code, table


def _in_intervals(pos: int, intervals: Sequence[Tuple[int, int]]) -> bool:
    for a, b in intervals:
        if a <= pos <= b:
            return True
    return False


def _locate(pos: int, heads: Sequence[Tuple[int, int, str, str]]) -> Tuple[str, str]:
    starts = [h[0] for h in heads]
    i = bisect.bisect_right(starts, pos) - 1
    if i < 0:
        return "", ""
    return heads[i][2], heads[i][3]


def _quote_around(text: str, pos: int, limit: int = 240) -> str:
    half = limit // 2
    a, b = max(0, pos - half), min(len(text), pos + half)
    q = text[a:b].replace("\n", " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _is_definitional(quote: str) -> bool:
    return bool(DEFINITION_RE.search(quote))


# ---------------------------------------------------------------- 候选生成
def _surface_ok(surface: str) -> bool:
    s = (surface or "").strip()
    if len(s) < 2 or len(s) > 64:
        return False
    if BAD_SURFACE_RE.search(s):
        return False
    if s.upper() in STOP_UPPER:
        return False
    first = s.split(" ", 1)[0]
    if first in STOP_TITLE:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    return True


def _add_mention(cands: Dict[str, Candidate], surface: str, key: str,
                 doc, body: str, pos: int, heads, code, table,
                 in_heading: bool = False) -> None:
    c = cands.get(key)
    if c is None:
        c = Candidate(key=key, surface=surface)
        cands[key] = c
    c.variants[surface] += 1
    c.mentions += 1
    c.docs.add(doc.doc_id)
    if doc.collection:
        c.collections.add(doc.collection)
    if in_heading:
        c.in_heading = True
    heading, path = _locate(pos, heads)
    if heading:
        c.headings.add(heading)
    quote = _quote_around(body, pos)
    ev = Evidence(
        doc_id=doc.doc_id, doc_title=doc.title,
        heading=heading or doc.title, heading_path=path or doc.title,
        quote=quote, offset=pos, collection=doc.collection,
        definitional=_is_definitional(quote),
        in_code=_in_intervals(pos, code),
        in_table=_in_intervals(pos, table),
    )
    c.code_mentions += 1 if ev.in_code else 0
    c.table_mentions += 1 if ev.in_table else 0
    c.definitional += 1 if ev.definitional else 0
    c.evidence.append(ev)


def _merge_candidate(dst: Candidate, src: Candidate) -> None:
    dst.variants.update(src.variants)
    dst.mentions += src.mentions
    dst.docs |= src.docs
    dst.collections |= src.collections
    dst.headings |= src.headings
    dst.in_heading = dst.in_heading or src.in_heading
    dst.code_mentions += src.code_mentions
    dst.table_mentions += src.table_mentions
    dst.definitional += src.definitional
    dst.evidence.extend(src.evidence)


def _surface_pattern(surface: str):
    """按写法匹配正文：空格/连字符/下划线视为同一分隔，大小写不敏感。"""
    parts = [p for p in re.split(r"[\s\-_·]+", surface or "") if p]
    if not parts:
        return None
    body = r"[\s\-_·]+".join(re.escape(p) for p in parts)
    return re.compile(r"(?i)(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")


def _heading_terms(text: str) -> List[str]:
    out: List[str] = []
    for m in LATIN_RE.finditer(text or ""):
        chunk = m.group(0)
        for part in re.split(r"\s*(?:[:：，,；;（）()【】\[\]|/])\s*", chunk):
            part = part.strip(" .-—_")
            if len(part) >= 2 and re.search(r"[A-Za-z]", part):
                out.append(part)
    return out


def generate_candidates(docs, lex: lexicon_mod.Lexicon,
                        doc_ids: Optional[Set[str]] = None) -> Dict[str, Candidate]:
    cands: Dict[str, Candidate] = {}
    heading_keys: Set[str] = set()

    for doc in docs:
        if doc_ids is not None and doc.doc_id not in doc_ids:
            continue
        body = doc.body
        heads, code, table = _scan_structure(body)

        # 标题词先登记，保证 in_heading 标记与正文命中都能合并
        for _, _, text, _ in heads:
            for term in _heading_terms(text):
                if not _surface_ok(term):
                    continue
                key = lexicon_mod.normalize_term(term)
                if not key:
                    continue
                heading_keys.add(key)
                c = cands.get(key)
                if c is None:
                    c = Candidate(key=key, surface=term)
                    cands[key] = c
                c.in_heading = True
                c.variants[term] += 1

        for pat in (ACRONYM_RE, LEVEL_RE, CAP_SEQ_RE, CAMEL_RE, HYPHEN_RE):
            for m in pat.finditer(body):
                surface = m.group(0).strip()
                if not _surface_ok(surface):
                    continue
                key = lexicon_mod.normalize_term(surface)
                if not key:
                    continue
                _add_mention(cands, surface, key, doc, body, m.start(),
                             heads, code, table)

    # 词表种子：即使正文没有正则命中，也要能进入候选
    for term in lex.seed_terms():
        if not _surface_ok(term):
            continue
        key = lexicon_mod.normalize_term(term)
        if not key:
            continue
        c = cands.get(key) or Candidate(key=key, surface=term)
        c.variants[term] += 1
        cands.setdefault(key, c)

    # 回扫正文：正则只抓大写缩写/驼峰/连字符，Istio、ztunnel、waypoint、
    # Ingress 这类单词型专名只靠标题或词表进来，如果不回扫，频次和证据都是空的，
    # 排序会被噪音挤到后面。只对「还没什么正文命中」的候选做，成本可控。
    by_doc = {d.doc_id: d for d in docs
              if doc_ids is None or d.doc_id in doc_ids}
    for c in list(cands.values()):
        if c.mentions > 1 and len(c.docs) >= 1:
            continue
        pat = _surface_pattern(c.surface)
        if pat is None:
            continue
        for doc in by_doc.values():
            body = doc.body
            if not pat.search(body):
                continue
            heads, code, table = _scan_structure(body)
            for m in pat.finditer(body):
                _add_mention(cands, c.surface, c.key, doc, body, m.start(),
                             heads, code, table)

    # 用词表把同义写法合并到 canonical（KMS / AWS KMS）
    merged: Dict[str, Candidate] = {}
    for key, c in list(cands.items()):
        entry = lex.find(c.surface)
        target_key = lexicon_mod.normalize_term(entry.canonical) if entry else key
        if target_key in merged:
            _merge_candidate(merged[target_key], c)
        else:
            if entry and target_key != key:
                old = c.surface
                c.surface = entry.canonical
                c.variants[entry.canonical] += c.variants.get(old, 0)
            merged[target_key] = c
            merged[target_key].key = target_key
    cands = merged

    # 词表是跨 collection 共用的；只把「本 collection 真的出现过」的种子留下。
    # 否则 ontology 只有 1 篇文档，却会把 VPC/Istio/IAM 这些别处的词也送进复核。
    cands = {k: c for k, c in cands.items()
             if c.mentions > 0 or c.in_heading}

    for c in cands.values():
        if not c.surface:
            c.surface = c.variants.most_common(1)[0][0]
        if c.in_heading:
            c.mentions += 1
        code_ratio = c.code_mentions / max(c.mentions, 1)
        c.score = (
            min(len(c.docs), 6) * 2.0
            + math.sqrt(min(c.mentions, 100)) * 0.8
            + (3.0 if c.in_heading else 0.0)
            + min(c.definitional, 3) * 1.0
            + (1.0 if len(c.headings) >= 2 else 0.0)
            + (4.0 if lex.find(c.surface) else 0.0)
            - (2.0 if code_ratio > 0.7 else 0.0)
            - (1.5 if len(c.surface.strip()) <= 2 else 0.0)
        )
        # 证据排序：定义句优先，再按文档/小节去重
        # 定义句优先，其次是散文（表格/代码里的“定义”往往是索引行或变量名）
        c.evidence.sort(key=lambda e: (not e.definitional, e.in_table, e.in_code,
                                       e.doc_id, e.offset))
        seen = set()
        picked = []
        for e in c.evidence:
            sig = (e.doc_id, contracts.norm_name(e.heading))
            if sig in seen:
                continue
            seen.add(sig)
            picked.append(e)
            if len(picked) >= 5:
                break
        c.evidence = picked
    return cands


# ---------------------------------------------------------------- 页面/注册表索引
@dataclass
class PageRef:
    slug: str
    title: str
    type: str
    matched: str
    via_alias: bool


def index_pages(pages) -> Dict[str, PageRef]:
    out: Dict[str, PageRef] = {}
    for p in pages:
        for name, via_alias in [(p.title, False), (p.slug, False)] + [
                (a, True) for a in p.aliases()]:
            key = contracts.norm_name(name)
            if key and key not in out:
                out[key] = PageRef(p.slug, p.title, p.type, name, via_alias)
    return out


def _strict_prefix(a: str, b: str) -> bool:
    return len(a) >= 3 and len(a) < len(b) and b.startswith(a)


def apply_gates(cands: Dict[str, Candidate], pages: Dict[str, PageRef],
                reg: registry_mod.Registry, lex: lexicon_mod.Lexicon) -> None:
    for key, c in cands.items():
        c.candidate_id = "%s:%s" % (key[:40], hashlib.md5(c.surface.encode()).hexdigest()[:6])
        entry = lex.find(c.surface)
        if entry:
            c.lexicon_entry = entry.as_dict()

        page = pages.get(key)
        if page:
            c.gate = "covered"
            c.gate_reason = "existing_page:%s" % page.slug
            c.existing_page = page.slug
            if page.via_alias and _strict_prefix(key, contracts.norm_name(page.title)):
                # 上位词挂在窄页上（Istio 挂在 istio-ambient）
                c.gate = "needs_review"
                c.gate_reason = "alias_hypernym:%s" % page.slug
                c.existing_page = page.slug
            c.proposed = {
                "action": "alias_only",
                "canonical": page.title,
                "type": page.type,
                "merge_into": page.slug,
                "aliases": [],
                "qualifier": None,
                "confidence": 0.95,
                "proposed_by": "heuristic",
            }
            continue

        # 通用缩写（API/HTTP/TCP/JSON…）只用于展开与检索，永远不建页。
        if entry and entry.kind == "common":
            c.gate = "observation"
            c.gate_reason = "common_abbreviation"
            continue

        reg_entry = reg.find(c.surface)
        if reg_entry is not None:
            if reg_entry.status == "rejected":
                c.gate = "observation"
                c.gate_reason = "registry_rejected"
            elif reg_entry.status == "merged":
                c.gate = "covered"
                c.gate_reason = "registry_merged:%s" % reg_entry.canonical
            elif reg_entry.type != "entity" and c.lexicon_entry and c.lexicon_entry.get("kind") == "proper":
                c.gate = "needs_review"
                c.gate_reason = "type_conflict"

        if c.gate == "covered":
            continue

        # G5 频次门槛
        if c.mentions < 2 and not c.in_heading and not c.lexicon_entry:
            c.gate = "observation"
            c.gate_reason = c.gate_reason or "single_mention"
            continue

        # G4 只有代码/表格证据：低信号的直接进观察池，不占用复核预算
        if c.evidence and all(e.in_code or e.in_table for e in c.evidence):
            if len(c.docs) < 2 and not c.in_heading and not c.lexicon_entry:
                c.gate = "observation"
                c.gate_reason = "code_only_low_signal"
                continue
            c.gate = "needs_review"
            c.gate_reason = "code_or_table_only"

        # G6 缩写未展开
        # 只对「短缩写」强制展开；NVIDIA/OWASP 这类长全大写词是品牌/组织名，
        # 不是需要展开的缩写，不能因为长度像缩写就卡住。
        is_acronym = (bool(ACRONYM_RE.fullmatch(c.surface)) and len(c.surface) <= 4) \
            or bool(LEVEL_RE.fullmatch(c.surface))
        expansion = ""
        if entry:
            expansion = entry.expansion or (entry.canonical if entry.kind == "acronym" else "")
            if not expansion:
                # 专有名词表里常把全称写在别名里（MCP → Model Context Protocol）
                expansion = next(
                    (a for a in entry.all_forms()
                     if a != entry.canonical and (" " in a or len(a) > len(entry.canonical) + 2)),
                    "")
        if reg_entry:
            expansion = expansion or reg_entry.acronym_of
        if is_acronym and not expansion:
            c.gate = "needs_review"
            c.gate_reason = "acronym_unexpanded"

        # G8/G9 与现有页面的包含冲突
        collisions = [
            p for k, p in pages.items()
            if len(k) >= 3 and (k in key or key in k) and k != key
        ]
        if len(collisions) >= 2:
            c.gate = "needs_review"
            c.gate_reason = "multi_collision"
        elif len(collisions) == 1 and not c.gate_reason:
            other = collisions[0]
            if _strict_prefix(key, contracts.norm_name(other.title)) or \
                    _strict_prefix(contracts.norm_name(other.title), key):
                c.gate = "needs_review"
                c.gate_reason = "near_duplicate:%s" % other.slug

        if c.gate != "needs_review":
            c.gate = "needs_review"
            c.gate_reason = c.gate_reason or "new_candidate"

        c.proposed = _heuristic_proposal(c, entry, reg_entry)


def _heuristic_proposal(c: Candidate, entry, reg_entry) -> Dict:
    canonical = entry.canonical if entry else (reg_entry.canonical if reg_entry else c.surface)
    aliases = []
    if entry:
        aliases = [x for x in entry.all_forms()
                   if lexicon_mod.normalize_term(x) != lexicon_mod.normalize_term(canonical)]
    if reg_entry:
        for a in reg_entry.all_forms():
            if a and lexicon_mod.normalize_term(a) != lexicon_mod.normalize_term(canonical) \
                    and a not in aliases:
                aliases.append(a)

    typ = "entity"
    if reg_entry and reg_entry.type in registry_mod.VALID_TYPES:
        typ = reg_entry.type
    elif entry and entry.kind in ("proper", "acronym"):
        typ = "entity"
    elif len(c.docs) >= 2 and c.definitional and not c.in_heading:
        typ = "concept"

    confidence = 0.35
    confidence += min(len(c.docs), 3) * 0.12
    confidence += min(c.definitional, 2) * 0.08
    confidence += 0.1 if c.in_heading else 0
    confidence += 0.08 if c.mentions >= 5 else 0
    confidence -= 0.15 if c.gate_reason.startswith("code_or_table") else 0
    confidence -= 0.15 if c.gate_reason.startswith("acronym_unexpanded") else 0
    confidence = max(0.05, min(0.92, confidence))

    action = "accept_new"
    if c.gate.startswith("alias_hypernym"):
        action = "accept_new"
    if c.mentions < 2 and len(c.docs) == 1 and c.definitional == 0:
        action = "defer"

    return {
        "action": action,
        "canonical": canonical,
        "type": typ,
        "aliases": aliases[:8],
        "merge_into": None,
        "qualifier": None,
        "confidence": round(confidence, 2),
        "proposed_by": "heuristic",
    }


# ---------------------------------------------------------------- 工单
PROPOSER_RULES = [
    "你只输出 JSON。每个候选给出 action / canonical / type / aliases / merge_into / qualifier / confidence。",
    "action 只能是 accept_new | merge_into | split | alias_only | reject | defer。",
    "type 只能是 entity（可指称的具体对象）| concept（机制/原则）| topic（跨页综述）。",
    "缩写必须先展开成全称，全称进 aliases；不得把缩写当作 canonical。",
    "同名不同义必须拆页，用 qualifier 区分（如 sts-security-token-service）。",
    "上位概念不得与下位对象合并（Istio ≠ Istio Ambient）。",
    "证据不足就 defer，不要猜。",
    "每个决定必须能在 evidence 里找到依据；不得引入证据之外的事实。",
]

CHALLENGER_RULES = [
    "你是对抗复核者。你的任务不是同意，而是**证伪**提案。",
    "你看不到提案者的置信度和理由，只能看提案字段与证据。",
    "逐项检查 12 条攻击清单，输出 objections（rule / severity / evidence / fix）。",
    "blocker 必须给出可执行替代方案（merge_into / qualifier / reject_reason），否则该反对无效。",
    "最终 verdict 只能是 uphold（维持）| revise（给出修订后的完整提案）| reject（整体否决）。",
    "不确定时 verdict=defer，不要为了显得严格而一概反对。",
    "只输出 JSON。",
]

ATTACK_CHECKLIST = [
    {"id": "A", "name": "证据充分性", "check": "是否有定义句，而非仅仅被提及；是否只出现在代码/表格"},
    {"id": "B", "name": "同一性", "check": "是否只是另一个候选的别名/缩写/中英写法"},
    {"id": "C", "name": "粒度", "check": "是否现有页的部件/模式/实例（应 merge_into）"},
    {"id": "D", "name": "类型", "check": "entity / concept / topic 是否判对"},
    {"id": "E", "name": "边界", "check": "定义与边界能否写清；是否过于泛化"},
    {"id": "F", "name": "同形异义", "check": "同名是否指不同东西；是否需要 qualifier"},
    {"id": "G", "name": "别名安全", "check": "该 alias 是否是上位词，会把查询带偏"},
    {"id": "H", "name": "重复", "check": "是否与现有页近重复（子串/超串/同义）"},
    {"id": "I", "name": "稳定性", "check": "是否版本号、一次性项目名、代码变量名"},
    {"id": "J", "name": "来源质量", "check": "证据是否来自机翻件或重复件"},
    {"id": "K", "name": "可解析", "check": "doc+heading 是否真实存在"},
    {"id": "L", "name": "矛盾", "check": "是否与现有页定义/数字/结论冲突"},
]


def build_propose_order(collection: str, cands: List[Candidate],
                        max_candidates: int) -> Dict:
    selected = [c for c in cands if c.gate == "needs_review"]
    selected.sort(key=lambda c: (-c.score, -len(c.docs), -c.mentions, c.key))
    # 词表里登记过的专名/缩写，即使正文命中少（NAT Gateway 只有 5 次），
    # 也必须进入本轮复核；否则会被高频噪音挤出前 N 条。
    priority = [c for c in selected
                if c.lexicon_entry
                or c.gate_reason.startswith("alias_hypernym")
                or c.gate_reason.startswith("type_conflict")]
    priority_ids = {id(c) for c in priority}
    rest = [c for c in selected if id(c) not in priority_ids]
    # 已登记的词表条目一个都不能丢：它们是「用户明确要求管理」的词。
    # 只有剩余预算才留给高分噪音候选。
    if len(priority) >= max_candidates:
        selected = priority
    else:
        selected = priority + rest[:max_candidates - len(priority)]
    return {
        "version": 1,
        "stage": "propose",
        "collection": collection,
        "rules": PROPOSER_RULES,
        "contract": {
            "output": "JSON 数组，每项含 candidate_id / action / canonical / type / aliases / merge_into / qualifier / confidence / evidence_used",
            "decision_values": ["accept_new", "merge_into", "split", "alias_only", "reject", "defer"],
            "type_values": list(registry_mod.VALID_TYPES),
        },
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "surface": c.surface,
                "mentions": c.mentions,
                "doc_coverage": len(c.docs),
                "in_heading": c.in_heading,
                "definitional": c.definitional,
                "gate_reason": c.gate_reason,
                "existing_page": c.existing_page,
                "lexicon": c.lexicon_entry,
                "evidence": [e.as_dict() for e in c.evidence[:5]],
            }
            for c in selected
        ],
    }


def build_challenge_order(collection: str, proposals: List[Dict],
                          candidates: Dict[str, Candidate]) -> Dict:
    """生成盲审工单：剥掉置信度、理由和 proposed_by。"""
    items = []
    for p in proposals:
        cid = p.get("candidate_id")
        c = candidates.get(cid) or next(
            (x for x in candidates.values() if x.candidate_id == cid), None)
        # 候选查找：candidates 以 key 为键，这里按 candidate_id 再匹配一次
        if c is None:
            c = _find_candidate(candidates, cid)
        items.append({
            "candidate_id": cid,
            "proposal": {
                "action": p.get("action"),
                "canonical": p.get("canonical"),
                "type": p.get("type"),
                "aliases": p.get("aliases") or [],
                "merge_into": p.get("merge_into"),
                "qualifier": p.get("qualifier"),
            },
            "evidence": [e.as_dict() for e in (c.evidence[:5] if c else [])],
            "existing_page": c.existing_page if c else "",
            "gate_reason": c.gate_reason if c else "",
        })
    return {
        "version": 1,
        "stage": "challenge",
        "collection": collection,
        "rules": CHALLENGER_RULES,
        "attack_checklist": ATTACK_CHECKLIST,
        "contract": {
            "output": "JSON 数组，每项含 candidate_id / verdict / objections / revised_proposal",
            "verdict_values": ["uphold", "revise", "reject", "defer"],
        },
        "items": items,
    }


def _find_candidate(candidates: Dict[str, Candidate], cid: str) -> Optional[Candidate]:
    for c in candidates.values():
        if c.candidate_id == cid:
            return c
    return None


def candidate_summary(cands: Iterable[Candidate]) -> Dict:
    counts = Counter(c.gate for c in cands)
    reasons = Counter(c.gate_reason.split(":")[0] for c in cands if c.gate_reason)
    return {
        "total": sum(counts.values()),
        "covered": counts.get("covered", 0),
        "observation": counts.get("observation", 0),
        "needs_review": counts.get("needs_review", 0),
        "reasons": dict(reasons.most_common(20)),
    }
