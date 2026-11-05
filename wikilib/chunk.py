"""自适应切分：按标题层级切，切不动再按段落切。

为什么不能直接用 AI-KG 的 `## 切分`
----------------------------------
试点语料的结构深度差异极大：`Security Best Practices` 只有 2 个 H2 却有 31 个 H4，
按 H2 切会得到两万字的巨块；而 `AI Agent 安全最佳实践` 用 H2 就刚好。
所以这里从最浅层往下试，直到块的中位长度落到目标区间。

三条硬约束
----------
1. **绝不切开围栏代码块**。语料里有 12 个 mermaid / bash 块。
2. 超长块按段落装箱；拆出的块只在 `heading_path` 上标 `(i/n)`，
   **不写进 heading** —— heading 要能当 Obsidian 锚点用。
3. 文档开头若有不属于任何标题的正文，单独成块，挂在文档标题下。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .normalize import NormalizedDoc

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

# 索引里 heading 列的截断长度。低置信度没修好的长标题会污染 BM25
# （heading 权重 10），所以进索引时截断，展示与锚点仍用原文。
INDEX_HEADING_MAX = 60


@dataclass
class Chunk:
    doc_id: str
    ord: int
    level: int
    heading: str
    heading_orig: str
    heading_path: str
    body: str
    chars: int
    chunk_hash: str
    part: str = ""                 # "1/3" 之类；进 heading_path，不进 heading

    @property
    def chunk_id(self) -> str:
        return "%s::%03d" % (self.doc_id, self.ord)

    @property
    def index_heading(self) -> str:
        h = self.heading.strip()
        return h if len(h) <= INDEX_HEADING_MAX else h[:INDEX_HEADING_MAX] + "…"


@dataclass
class _Head:
    level: int
    text: str
    orig: str
    start: int
    end: int


def _scan_headings(body: str) -> List[_Head]:
    heads: List[_Head] = []
    offset = 0
    in_fence = False
    for line in body.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = HEADING_RE.match(line)
            if m:
                heads.append(_Head(level=len(m.group(1)), text=m.group(2).strip(),
                                   orig=m.group(2).strip(),
                                   start=offset, end=offset + len(line)))
        offset += len(line) + 1
    return heads


def _ancestors(heads: List[_Head], idx: int) -> List[str]:
    """当前标题的祖先链（由外到内）。"""
    chain: List[str] = []
    level = heads[idx].level
    for j in range(idx - 1, -1, -1):
        if heads[j].level < level:
            chain.append(heads[j].text)
            level = heads[j].level
    return list(reversed(chain))


def _sections(body: str, heads: List[_Head], cut_level: int) -> List[Tuple[Optional[int], int, int]]:
    """按 level <= cut_level 的标题切分，返回 [(head_idx|None, start, end)]。"""
    cuts = [i for i, h in enumerate(heads) if h.level <= cut_level]
    out: List[Tuple[Optional[int], int, int]] = []

    first = cuts[0] if cuts else None
    if first is None or heads[first].start > 0:
        pre = body[:heads[first].start] if first is not None else body
        if pre.strip():
            out.append((None, 0, len(pre)))

    for k, hi in enumerate(cuts):
        start = heads[hi].start
        end = heads[cuts[k + 1]].start if k + 1 < len(cuts) else len(body)
        out.append((hi, start, end))
    return out


def _stats(sizes: List[int]) -> Tuple[int, int]:
    if not sizes:
        return 0, 0
    s = sorted(sizes)
    return s[len(s) // 2], s[-1]


def _split_blocks(text: str) -> List[str]:
    """按空行分块，但**不切断围栏代码块**。"""
    blocks: List[str] = []
    buf: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and not line.strip():
            if buf:
                blocks.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return blocks


def pack_parts(text: str, limit: int) -> List[str]:
    """把超长正文按段落装箱到 limit 以内；单块仍超长才硬切。"""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    cur: List[str] = []
    size = 0
    for block in _split_blocks(text):
        if len(block) > limit:
            if cur:
                parts.append("\n\n".join(cur))
                cur, size = [], 0
            for i in range(0, len(block), limit):
                parts.append(block[i:i + limit])
            continue
        if size + len(block) + 2 > limit and cur:
            parts.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return parts


def chunk_document(doc: NormalizedDoc, cfg) -> List[Chunk]:
    body = doc.body
    heads = _scan_headings(body)
    target = int(cfg.get("max_chunk_chars"))
    hard = int(cfg.get("hard_chunk_chars"))
    max_level = int(cfg.get("max_heading_level"))

    levels = sorted({h.level for h in heads} | {max_level})
    chosen = max_level
    for lvl in levels:
        if lvl > max_level:
            continue
        sections = _sections(body, heads, lvl)
        sizes = [text_len for _, s, e in sections if (text_len := e - s) > 0]
        median, biggest = _stats(sizes)
        # 只看中位数会被骗：3 个章节里 2 个很小、1 个 4 万字时中位数仍然很小。
        # 必须同时约束最大值，否则巨型章节会被整体切碎成一堆同名块。
        if median and median <= target and biggest <= hard:
            chosen = lvl
            break
        chosen = max_level

    chunks: List[Chunk] = []
    for hi, start, end in _sections(body, heads, chosen):
        raw = body[start:end]
        if hi is None:
            title = doc.title
            orig = doc.title
            level = 0
            path = doc.title
            text = raw
        else:
            head = heads[hi]
            title = head.text
            orig = head.orig
            level = head.level
            path = " > ".join([doc.title] + _ancestors(heads, hi) + [head.text])
            text = body[head.end:end]
        text = text.strip()
        if not text:
            continue

        pieces = [text] if len(text) <= hard else pack_parts(text, target)
        for i, piece in enumerate(pieces, 1):
            part = "" if len(pieces) == 1 else "%d/%d" % (i, len(pieces))
            chunks.append(Chunk(
                doc_id=doc.doc_id,
                ord=len(chunks),
                level=level,
                heading=title,
                heading_orig=orig,
                heading_path=path + ("（%s）" % part if part else ""),
                body=piece,
                chars=len(piece),
                chunk_hash=hashlib.md5(piece.encode("utf-8")).hexdigest(),
                part=part,
            ))
    return chunks


def chunk_collection(docs: List[NormalizedDoc], cfg) -> Dict[str, List[Chunk]]:
    return {d.doc_id: chunk_document(d, cfg) for d in docs}
