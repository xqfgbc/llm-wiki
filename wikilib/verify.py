"""确定性校验：哪些规则能纯 Python 判定，哪些必须交给人。

原则：**确定性规则决定「能不能发布」，LLM 只在需要人看的时候才参与。**
撰写和 lint 用同一个模型，错误会相关、互相背书，所以引用解析这类
机械判定一律走 SQL 与正则，不调 LLM。
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import contracts
from . import glossary as glossary_mod

ERROR, WARN = "error", "warn"


@dataclass
class Result:
    rule: str
    severity: str
    slug: str
    message: str


@dataclass
class Snapshot:
    """规范化快照的标题索引：doc_id -> {norm: orig}。"""
    docs: Dict[str, Dict[str, str]] = field(default_factory=dict)
    hashes: Dict[str, Dict[str, str]] = field(default_factory=dict)   # doc_id -> norm -> hash
    titles: Dict[str, str] = field(default_factory=dict)      # 归一化标题 -> doc_id
    headings_all: Set[Tuple[str, str]] = field(default_factory=set)
    origins: Dict[str, str] = field(default_factory=dict)   # doc_id -> 源文件绝对路径
    mtime: float = 0.0

    def resolve(self, doc_ref: str, heading: str
                ) -> Tuple[Optional[str], Optional[str], str]:
        """返回 (doc_id, orig_heading, 状态)。状态：ok / no_doc / no_heading。"""
        doc_id = self.titles.get(contracts.norm_name(doc_ref)) or (
            doc_ref if doc_ref in self.docs else None)
        if not doc_id:
            return None, None, "no_doc"
        orig = self.docs[doc_id].get(contracts.norm_name(heading))
        if orig is None:
            return doc_id, None, "no_heading"
        return doc_id, orig, "ok"


def _sections_text(body: str):
    """按 H2 切分，返回 [(标题, 正文)]。"""
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.M)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    return out


def load_snapshot(raw_dir: str) -> Snapshot:
    snap = Snapshot()
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.map.json"))):
        data = json.load(open(path, encoding="utf-8"))
        doc_id = data["doc_id"]
        heads, hashes = {}, {}
        for h in data.get("headings", []):
            key = contracts.norm_name(h["norm"])
            heads[key] = h["orig"]
            if h.get("hash"):
                hashes[key] = h["hash"]
        snap.docs[doc_id] = heads
        snap.hashes[doc_id] = hashes
        snap.mtime = max(snap.mtime, os.path.getmtime(path))

    # 标题（frontmatter.title）也要能作为 doc_ref 使用。
    # ⚠️ 必须用 frontmatter 解析：长标题会被 YAML 折行，逐行正则只能拿到第一行。
    import frontmatter as _fm
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.md"))):
        post = _fm.loads(open(path, encoding="utf-8").read())
        doc_id = os.path.splitext(os.path.basename(path))[0]
        title = str((post.metadata or {}).get("title") or "").strip()
        if title:
            snap.titles[contracts.norm_name(title)] = doc_id
        snap.titles[contracts.norm_name(doc_id)] = doc_id
        origin = str((post.metadata or {}).get("source_path") or "")
        if origin:
            snap.origins[doc_id] = origin
    return snap


def load_pages(pages_dir: str) -> List[contracts.Page]:
    paths = sorted(glob.glob(os.path.join(pages_dir, "**", "*.md"), recursive=True))
    return [contracts.parse_page(p) for p in paths
            if os.path.basename(p).upper() not in ("CLAUDE.MD", "INDEX.MD", "LOG.MD")]


def verify_page(page: contracts.Page, snap: Snapshot, gl: glossary_mod.Glossary,
                max_page_chars: int, min_cjk: float) -> List[Result]:
    out: List[Result] = []

    # ① 引用可解析
    for cite in page.cites:
        doc_id, orig, status = snap.resolve(cite.doc, cite.heading)
        if status == "no_doc":
            out.append(Result("cite_unresolved", ERROR, page.slug,
                              "引用的文档不存在：%s（第 %d 行）" % (cite.doc, cite.line)))
        elif status == "no_heading":
            out.append(Result("cite_unresolved", ERROR, page.slug,
                              "文档 %s 里没有小节「%s」（第 %d 行）"
                              % (cite.doc, cite.heading, cite.line)))

    # ② 结构与元数据
    is_note = page.type == "note"
    required = contracts.REQUIRED_NOTE_META if is_note else contracts.REQUIRED_META
    # 用「键是否存在」而不是「值是否非空」：笔记允许 sources 为空列表，
    # 那恰恰表示「这条判断没有原文依据」。
    missing = [k for k in required if k not in page.meta]
    if not is_note and not page.meta.get("sources"):
        missing.append("sources（结构化页必须至少声明一个来源）")
    if missing:
        out.append(Result("meta_missing", ERROR, page.slug,
                          "frontmatter 缺少：%s" % ", ".join(missing)))
    if page.meta.get("type") not in contracts.VALID_TYPES:
        out.append(Result("type_invalid", ERROR, page.slug,
                          "type 非法：%r" % page.meta.get("type")))
    if page.status not in contracts.VALID_STATUS:
        out.append(Result("status_invalid", ERROR, page.slug,
                          "status 非法：%r" % page.status))
    if is_note:
        if page.meta.get("origin") not in contracts.NOTE_ORIGINS:
            out.append(Result("origin_invalid", ERROR, page.slug,
                              "笔记的 origin 必须是 %s 之一，实际 %r"
                              % ("/".join(contracts.NOTE_ORIGINS),
                                 page.meta.get("origin"))))
        if not page.sections:
            out.append(Result("structure", ERROR, page.slug,
                              "笔记至少需要一个二级标题"))
    elif page.sections != contracts.SECTIONS:
        have, want = page.sections, contracts.SECTIONS
        out.append(Result("structure", ERROR, page.slug,
                          "H2 序列不符。期望 %d 段 %s；实际 %d 段 %s"
                          % (len(want), want, len(have), have)))

    # 每段至少一条引用（结构化页）。笔记豁免：它写的正是原文里没有的判断。
    # 但笔记若声明了来源，就必须真的引用到，否则前后不一致。
    if not is_note:
        EXEMPT = set(contracts.NO_CITATION_SECTIONS)
        for name, text in _sections_text(page.body):
            if name in EXEMPT:
                continue
            if not contracts.CITE_RE.search(text):
                out.append(Result("section_no_citation", ERROR, page.slug,
                                  "「%s」一节没有任何引用" % name))
    elif page.source_docs() and not page.cites:
        out.append(Result("note_declared_no_cite", ERROR, page.slug,
                          "笔记声明了 sources 但正文没有任何引用"))

    h1 = re.findall(r"^#\s+(.+)$", page.body, re.M)
    if not h1 or h1[0].strip() != page.title.strip():
        out.append(Result("h1_mismatch", ERROR, page.slug,
                          "H1 必须唯一且等于 frontmatter.title"))

    # ③ 中文占比
    ratio, cjk, latin = contracts.cjk_ratio(page.body, gl.keep_en)
    if ratio < min_cjk:
        out.append(Result("cjk_ratio", ERROR, page.slug,
                          "中文占比 %.2f < %.2f（CJK %d / Latin %d）"
                          % (ratio, min_cjk, cjk, latin)))

    # ④ 禁用译法（代码块不计入，配置示例里出现英文术语是正常的）
    prose = re.sub(r"```.*?```", " ", page.body, flags=re.S)
    for bad in gl.banned:
        if bad and bad in prose:
            out.append(Result("banned_term", ERROR, page.slug,
                              "使用了禁用译法「%s」" % bad))

    # ⑤ 页面尺寸
    size = contracts.body_chars(page.body)
    if size > max_page_chars:
        out.append(Result("page_too_long", ERROR, page.slug,
                          "正文 %d 字 > %d；请用链接下钻拆分" % (size, max_page_chars)))

    # ⑥ 引用与 sources 的一致性
    declared = {(contracts.norm_name(s.get("doc", "")),
                 contracts.norm_name(c.get("heading", "")))
                for s in page.source_docs() for c in (s.get("cites") or [])}
    cited = {(contracts.norm_name(c.doc), contracts.norm_name(c.heading))
             for c in page.cites}
    for doc, heading in sorted(cited - declared):
        out.append(Result("undeclared_citation", ERROR, page.slug,
                          "正文引用未在 sources 声明：%s#%s" % (doc, heading)))
    for doc, heading in sorted(declared - cited):
        out.append(Result("unused_source", WARN, page.slug,
                          "sources 声明了但正文没引用：%s#%s" % (doc, heading)))

    # ⑧ 编译时间（只校验可解析；「是否陈旧」由 cites[] 的内容 hash 判断，
    #    不能拿快照重建时间比——那样每重建一次全站都会误报陈旧）
    ts = str(page.meta.get("compiled_at") or "")
    try:
        _dt.datetime.fromisoformat(ts)
    except ValueError:
        out.append(Result("compiled_at", ERROR, page.slug,
                          "compiled_at 无法解析：%r" % ts))

    return out


def verify_collection(pages: List[contracts.Page], snap: Snapshot,
                      gl: glossary_mod.Glossary, cfg) -> List[Result]:
    out: List[Result] = []
    max_chars = int(cfg.get("max_page_chars"))
    min_cjk = float(cfg.get("min_cjk_ratio"))

    for page in pages:
        out.extend(verify_page(page, snap, gl, max_chars, min_cjk))

    # ⑦ 命名冲突
    seen_slug: Dict[str, str] = {}
    seen_name: Dict[str, str] = {}
    for page in pages:
        if page.slug in seen_slug:
            out.append(Result("duplicate_slug", ERROR, page.slug,
                              "slug 重复：%s 与 %s" % (seen_slug[page.slug], page.path)))
        seen_slug.setdefault(page.slug, page.path)
        for name in [page.title] + page.aliases():
            key = contracts.norm_name(name)
            if not key:
                continue
            if key in seen_name and seen_name[key] != page.path:
                out.append(Result("duplicate_name", ERROR, page.slug,
                                  "名称/别名冲突：「%s」同时用于 %s 与 %s"
                                  % (name, seen_name[key], page.path)))
            seen_name.setdefault(key, page.path)

    # ⑨ 孤儿页 / ⑩ 断链
    known = {contracts.norm_name(p.slug) for p in pages}
    known |= {contracts.norm_name(p.title) for p in pages}
    known |= set(snap.titles)          # 指向源文档的链接不算断链
    for p in pages:
        for a in p.aliases():
            known.add(contracts.norm_name(a))

    inbound: Dict[str, int] = {}
    for page in pages:
        for m in contracts.WIKILINK_RE.finditer(page.body):
            target = contracts.norm_name(m.group(1))
            if not target:
                continue
            if target in known:
                inbound[target] = inbound.get(target, 0) + 1
            elif not m.group(2):          # 指向源文档的链接不算断链
                out.append(Result("red_link", WARN, page.slug,
                                  "未解析的链接：[[%s]]" % m.group(1)))

    for page in pages:
        if page.type == "note":
            continue
        if contracts.norm_name(page.title) not in inbound and \
                contracts.norm_name(page.slug) not in inbound:
            out.append(Result("orphan", WARN, page.slug, "没有其他页链接到本页"))

    # ⑪ 陈旧：sources 里的 hash 与当前快照比对
    for page in pages:
        for s in page.source_docs():
            ref = s.get("doc", "")
            doc = snap.titles.get(contracts.norm_name(ref), ref)
            cur = snap.docs.get(doc)
            if cur is None:
                out.append(Result("stale", WARN, page.slug,
                                  "来源文档已不在快照中：%s" % doc))
                continue
            for c in (s.get("cites") or []):
                h = contracts.norm_name(c.get("heading", ""))
                if h not in cur:
                    out.append(Result("stale", WARN, page.slug,
                                      "来源小节已消失：%s#%s" % (doc, c.get("heading"))))
                    continue
                old = str(c.get("hash") or "")
                now = snap.hashes.get(doc, {}).get(h, "")
                if old and now and old != now:
                    out.append(Result("stale", WARN, page.slug,
                                      "来源内容已变更：%s#%s" % (doc, c.get("heading"))))
                elif not old:
                    out.append(Result("unstamped", WARN, page.slug,
                                      "未记录内容 hash，无法判断是否陈旧：%s#%s"
                                      % (doc, c.get("heading"))))
    return out


def summarize(results: List[Result]) -> Tuple[int, int]:
    err = sum(1 for r in results if r.severity == ERROR)
    warn = sum(1 for r in results if r.severity == WARN)
    return err, warn
