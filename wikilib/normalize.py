"""源规范化：把网页剪藏整理成可编译的快照。

**源目录只读。** 输出写 `data/raw/<collection>/`，源文件一个字节都不改。

七步
----
N1 标题断行修复   剪藏常把标题和正文首句粘在一起（`### 新的攻击面STS 将所有…`），
                  这种标题会进 FTS 的 heading 列（BM25 权重 10），必须修好。
                  只做「断行」，不改写任何字符；低置信度只报告不落盘。
N2 去 Markdown 格式  标题里的 `**` / `*` / 反引号会让锚点对不上。
N3 噪音剔除       目录 / Contents / Further Reading 这类剪藏残留章节整节删除。
N4 重复件排除     由 exclusions 决定，这里只记录。
N5 frontmatter 补齐  无头部的文件补 title / source。
N6 标题映射       记录 规范化标题 → 原始标题，供深链锚点使用。
N7 图片引用修复   `![[粘贴图像<ts>.png]]` → 实际存在的 `Pasted image <ts>.png`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import frontmatter
import yaml

from . import config as cfgmod
from . import exclusions as exclmod

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
DIGITS_RE = re.compile(r"\d{8,}")

# 粘连的可疑断点。取**最早**的一个，而不是按规则优先级——
# 否则 `New Attack SurfaceThe STS … in a database. Default …` 会切在句号处，
# 标题变成一整句，比不修还糟。
# 断点规则带上名字：不同规则对「溢出部分」的形态要求不同，
# 用同一把尺子会误伤（见下面 camel 的说明）。
CANDIDATES = (
    ("punct", re.compile(r"[。！？；]")),
    ("quote", re.compile(r"[”\"』」](?=[A-Z一-鿿])")),
    ("paren", re.compile(r"\)(?=[A-Za-z一-鿿])")),
    ("camel", re.compile(r"[a-z](?=[A-Z])")),
    ("cjk_en", re.compile(r"[一-鿿](?=[A-Z]{2,})")),
    ("dash", re.compile(r"(?<=[A-Za-z0-9])[-–—](?=\s)")),
)
# camel 断点的两道门槛，用来把「真粘连」和「专名」分开：
#   1. 溢出必须以大写开头的正常单词起头（排除 SaaS → `Saa|S`）
#   2. 溢出里必须出现小写开头的词——真句子会有，标题式大写短语不会有
#      （排除 `ServiceNow Reference Architecture` → `Service|Now Reference…`）
CAMEL_SPILL = re.compile(r"^[A-Z][a-z]{2,}\b")
LOWER_WORD = re.compile(r"\b[a-z]{2,}\b")


def _camel_ok(spill: str) -> bool:
    spill = spill.strip()
    if not CAMEL_SPILL.match(spill):
        return False
    first = spill.split(" ", 1)[0]
    rest = spill[len(first):]
    return bool(LOWER_WORD.search(rest))

MIN_TITLE, MAX_TITLE = 4, 60
# 切出来的「标题」不能只是编号或单个字母（`A.` / `01.` / `iv.`）
ENUM_ONLY = re.compile(r"^\s*(?:[A-Za-z]|[IVXivx]{1,4}|\d{1,3})\s*[.)、]?\s*$")


@dataclass
class HeadingRec:
    level: int
    orig: str
    norm: str
    line: int
    repaired: bool = False
    confidence: str = "none"      # high | low | none
    synthetic: bool = False       # 由独立加粗行提升而来，原文里没有对应标题


@dataclass
class ImageRef:
    ref: str
    resolved: str = ""
    ok: bool = True


@dataclass
class NormalizedDoc:
    doc_id: str
    rel_path: str
    source_path: str
    sha256: str
    title: str
    source_url: str
    body: str                     # 规范化后的正文（不含 frontmatter）
    collection: str = ""
    headings: List[HeadingRec] = field(default_factory=list)
    images: List[ImageRef] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0
    low_confidence: List[Tuple[str, str]] = field(default_factory=list)

    def to_markdown(self) -> str:
        meta = {
            "title": self.title,
            "doc_id": self.doc_id,
            "collection": self.collection,
            "source": self.source_url,
            "source_path": self.source_path,
            "source_sha256": self.sha256,
            "normalized_by": "llm-wiki/normalize",
        }
        head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        return "---\n%s\n---\n\n%s\n" % (head, self.body.strip())


# ------------------------------------------------------------------ 标题处理
def strip_heading_markup(text: str) -> str:
    """去掉标题里的 Markdown 强调标记（N2）。"""
    t = text.strip()
    t = re.sub(r"^(\*{1,3}|_{1,3})(.+?)(\*{1,3}|_{1,3})$", r"\2", t)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"`([^`]*)`", r"\1", t)
    return t.strip()


def _split_points(text: str) -> List[int]:
    """所有可疑断点（每个都是「标题结束」的位置）。"""
    pts = set()
    for name, pat in CANDIDATES:
        for m in pat.finditer(text):
            pos = m.end()
            if pos < MIN_TITLE or pos >= len(text):
                continue
            if name == "camel" and not _camel_ok(text[pos:]):
                continue
            pts.add(pos)
    return sorted(pts)


def repair_heading(text: str, max_len: int, min_spill: int) -> Tuple[str, str, str]:
    """返回 (标题, 溢出正文, 置信度)。

    置信度：
      high —— 断点合法且标题/溢出长度都合理，可自动落盘
      low  —— 看起来粘连，但切出来的标题不合理，留给人工
      none —— 没有粘连迹象（可能只是标题本身长），原样保留、不告警
    """
    if len(text) <= max_len:
        return text, "", "none"
    points = _split_points(text)
    if not points:
        return text, "", "none"
    for pos in points:
        title, spill = text[:pos].strip(), text[pos:].strip()
        if not (MIN_TITLE <= len(title) <= MAX_TITLE):
            continue
        if ENUM_ONLY.match(title):
            continue
        if len(spill) < min_spill:
            continue
        return title, spill, "high"
    return text, "", "low"


# ------------------------------------------------------------------ 噪音
BOLD_LINE = re.compile(r"^\s*\*\*([^*]{4,40})\*\*\s*$")


def _promote_bold_headings(lines: List[str]):
    """把独立加粗行提升为二级标题，返回 (新行列表, 提升数量)。

    排除以标点结尾的引导句（`**主要内容包括以下几个部分：**`），
    它们后面跟的是列表，不是章节。
    """
    out, n = [], 0
    for line in lines:
        m = BOLD_LINE.match(line)
        if m and not m.group(1).strip().endswith(("：", ":", "，", ",", "、", "。", "…")):
            out.append("## " + m.group(1).strip())
            n += 1
        else:
            out.append(line)
    return out, n


def _is_noise_heading(text: str) -> bool:
    return bool(re.match(r"^(目录|contents|books i.?ve written|further reading|"
                         r"进一步阅读|延伸阅读|参考资料)$", text.strip(), re.I))


def _drop_leading_boilerplate(lines: List[str]) -> Tuple[List[str], int]:
    """删掉正文开头 `> Documentation Index … llms.txt` 这类抓取提示块。"""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    start = i
    block = []
    while i < len(lines) and lines[i].lstrip().startswith(">"):
        block.append(lines[i])
        i += 1
    if block and any(("llms.txt" in b or "Documentation Index" in b) for b in block):
        rest = lines[i:]
        while rest and not rest[0].strip():
            rest.pop(0)
        return rest, len(block)
    return lines, 0


# ------------------------------------------------------------------ 图片
def _image_index(source_dir: str) -> Dict[str, str]:
    """时间戳 → 文件名，用于修复 `粘贴图像<ts>` 这类错名引用。"""
    index: Dict[str, str] = {}
    try:
        names = os.listdir(source_dir)
    except OSError:
        return index
    for name in names:
        digits = DIGITS_RE.search(name)
        if digits:
            index.setdefault(digits.group(0), name)
    return index


def _fix_images(body: str, source_dir: str) -> Tuple[str, List[ImageRef]]:
    existing = set()
    try:
        existing = set(os.listdir(source_dir))
    except OSError:
        pass
    by_ts = _image_index(source_dir)
    refs: List[ImageRef] = []

    def repl(m: re.Match) -> str:
        ref = m.group(1).strip()
        if ref in existing:
            refs.append(ImageRef(ref=ref, resolved=ref, ok=True))
            return m.group(0)
        ts = DIGITS_RE.search(ref)
        if ts and ts.group(0) in by_ts:
            resolved = by_ts[ts.group(0)]
            refs.append(ImageRef(ref=ref, resolved=resolved, ok=True))
            return "![[%s]]" % resolved
        refs.append(ImageRef(ref=ref, resolved="", ok=False))
        return m.group(0)

    return IMAGE_RE.sub(repl, body), refs


# ------------------------------------------------------------------ 主流程
def normalize_document(path: str, rel_path: str, collection: str,
                       ex: exclmod.Exclusions, cfg: cfgmod.WikiConfig) -> NormalizedDoc:
    raw = open(path, encoding="utf-8").read()
    post = frontmatter.loads(raw)
    meta = dict(post.metadata or {})
    body = post.content or ""
    bytes_before = len(raw.encode("utf-8"))

    doc_id = os.path.splitext(os.path.basename(path))[0]
    title = str(meta.get("title") or "").strip()
    source_url = str(meta.get("source") or "").strip()

    # 标题粘连是**网页剪藏**的产物：剪藏器把标题和正文首句挤进了同一行。
    # PPT 派生的文档、手写笔记不会这样，强行修复只会误伤
    # （实测在知识库文档上把 `ServiceNow …` 切成 `Service`、把
    #   `AI Leadership & Op. Model …` 切成 `AI Leadership & Op.`）。
    tags = meta.get("tags") or []
    tags = [str(x).lower() for x in (tags if isinstance(tags, list) else [tags])]
    is_clipping = "clippings" in tags or source_url.lower().startswith("http")

    lines = body.split("\n")
    lines, _ = _drop_leading_boilerplate(lines)

    # N8：公众号类剪藏常常整篇没有 Markdown 标题，小节只用独立加粗行表示。
    # 这种情况按段落切会把主题打散，所以把「独占一行、长度合理、不是引导句」的
    # 加粗行提升为二级标题。只在这种文档上做，有标题的文档一律不动。
    promoted = 0
    if is_clipping and not any(HEADING_RE.match(l) for l in lines):
        lines, promoted = _promote_bold_headings(lines)

    max_len = int(cfg.get("heading_repair_max_len"))
    min_spill = int(cfg.get("heading_repair_min_spill"))

    out: List[str] = []
    headings: List[HeadingRec] = []
    dropped: List[str] = []
    low_conf: List[Tuple[str, str]] = []

    in_fence = False
    skip_until_level: Optional[int] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue

        m = None if in_fence else HEADING_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        level = len(m.group(1))
        orig_text = m.group(2).strip()

        # 正在跳过噪音章节：直到同级或更高级标题
        if skip_until_level is not None:
            if level > skip_until_level:
                i += 1
                continue
            skip_until_level = None

        clean = strip_heading_markup(orig_text)

        # N3 噪音章节
        noise = ex.match_heading(collection, clean) or (
            "剪藏噪音章节" if _is_noise_heading(clean) else None)
        if noise:
            dropped.append("%s（行 %d）：%s" % (clean, i + 1, noise))
            skip_until_level = level
            i += 1
            continue

        # N1 断行修复：只对网页剪藏生效
        if is_clipping:
            fixed, spill, confidence = repair_heading(clean, max_len, min_spill)
        else:
            fixed, spill, confidence = clean, "", "none"
        repaired = confidence == "high" and fixed != clean
        if confidence == "low":
            low_conf.append((clean, "无法自动断行，需人工判断"))
        if repaired:
            out.append("%s %s" % ("#" * level, fixed))
            if spill:
                out.append("")
                out.append(spill)
        else:
            out.append("%s %s" % ("#" * level, clean))

        # 整篇提升过的那批标题，原文里都没有对应锚点。
        # 早先写成 `synthetic = (orig_text == "")`，但 orig_text 永远非空，
        # 导致锚点仍然指向不存在的位置（测试抓到的）。
        headings.append(HeadingRec(
            level=level,
            orig="" if promoted else orig_text,
            norm=fixed, line=i + 1, repaired=repaired,
            confidence=confidence, synthetic=bool(promoted)))
        i += 1

    body = "\n".join(out)
    body, images = _fix_images(body, os.path.dirname(path))

    if not title:
        h1 = next((h.norm for h in headings if h.level == 1), "")
        title = h1 or doc_id

    doc = NormalizedDoc(
        doc_id=doc_id,
        rel_path=rel_path,
        source_path=path,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        title=title,
        source_url=source_url,
        body=body,
        collection=collection,
        headings=headings,
        images=images,
        dropped=dropped,
        bytes_before=bytes_before,
        bytes_after=len(body.encode("utf-8")),
        low_confidence=low_conf,
    )
    return doc


@dataclass
class ScanReport:
    docs: List[NormalizedDoc] = field(default_factory=list)
    excluded: List[Tuple[str, str, str]] = field(default_factory=list)   # (path, kind, reason)
    low_confidence: List[Tuple[str, str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _skip_roots(cfg: cfgmod.WikiConfig, collection: cfgmod.CollectionConfig):
    """扫描时要跳过的子树。

    1. wiki 根：那是派生物，不能当事实来源。vault 根已经在校验阶段被拒，
       这里再挡一次是为了防止手改配置把 wiki_root 放进某个源目录里。
    2. 其它 collection 已登记的源：更具体的登记优先，避免同一份文档被两个
       collection 各索引一次（例如同时登记了 obsidian/笔记 与 obsidian/笔记/云架构）。
    """
    skip = [os.path.realpath(cfg.obsidian.wiki_root)]
    for other in cfg.collections:
        if other.name == collection.name:
            continue
        skip.extend(os.path.realpath(s) for s in other.sources)
    return skip


def _under(path: str, roots) -> bool:
    real = os.path.realpath(path)
    for r in roots:
        if real == r or real.startswith(r + os.sep):
            return True
    return False


def scan_collection(collection: cfgmod.CollectionConfig, cfg: cfgmod.WikiConfig,
                    ex: exclmod.Exclusions) -> ScanReport:
    report = ScanReport()
    seen: Dict[str, str] = {}
    skip_roots = _skip_roots(cfg, collection)

    for root in collection.sources:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".")
                and not _under(os.path.join(dirpath, d), skip_roots))
            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue
                abs_path = os.path.join(dirpath, name)
                if _under(abs_path, skip_roots):
                    continue
                rel = os.path.relpath(abs_path, root).replace(os.sep, "/")

                rule = ex.match_file(collection.name, rel)
                if rule:
                    report.excluded.append((rel, "file", rule.reason))
                    continue

                doc = normalize_document(abs_path, rel, collection.name, ex, cfg)
                if doc.doc_id in seen:
                    report.warnings.append(
                        "doc_id 冲突：%s（%s 与 %s），已用相对路径去重"
                        % (doc.doc_id, seen[doc.doc_id], rel))
                    doc.doc_id = rel.replace("/", "__")[:-3]
                seen[doc.doc_id] = rel

                ratio = 0.0
                if doc.bytes_before:
                    ratio = 1 - doc.bytes_after / doc.bytes_before
                if ratio > float(cfg.get("warn_delete_ratio")):
                    report.warnings.append(
                        "%s 被删除了 %.0f%%，超过阈值；若属故意，请在排除名单里写明文件级条目的 reason"
                        % (rel, ratio * 100))

                report.docs.append(doc)
                for h, why in doc.low_confidence:
                    report.low_confidence.append((rel, h, why))

    report.docs.sort(key=lambda d: d.doc_id)
    return report


def write_raw(docs: List[NormalizedDoc], collection: str, cfg: cfgmod.WikiConfig) -> str:
    """写规范化快照 + 标题映射表，返回目录。"""
    out_dir = os.path.join(cfg.raw_dir, collection)
    os.makedirs(out_dir, exist_ok=True)
    for doc in docs:
        with open(os.path.join(out_dir, doc.doc_id + ".md"), "w", encoding="utf-8") as f:
            f.write(doc.to_markdown())
        sections = dict(_section_texts(doc.body))
        mapping = [{
            "level": h.level, "norm": h.norm, "orig": h.orig,
            "line": h.line, "repaired": h.repaired,
            "orig": "" if h.synthetic else h.orig,
            "synthetic": h.synthetic,
            "hash": hashlib.md5(
                ("%s\x00%s" % (h.orig, sections.get(h.norm, "")))
                .encode("utf-8")).hexdigest()[:8],
        } for h in doc.headings]
        with open(os.path.join(out_dir, doc.doc_id + ".map.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"doc_id": doc.doc_id, "headings": mapping}, f,
                      ensure_ascii=False, indent=1)
    return out_dir


def _section_texts(body: str):
    """按 H2/H3 切出每一节的正文，用于计算小节内容 hash。"""
    lines = body.split("\n")
    out, cur, buf = [], "", []
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            if cur:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if cur:
        out.append((cur, "\n".join(buf).strip()))
    return out
