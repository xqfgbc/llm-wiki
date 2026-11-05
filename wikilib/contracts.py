"""wiki 页的契约：解析、结构、引用。

引用的唯一合法写法：

    【[[文档标题#小节标题]]】

- `文档标题` 与快照的 `title` 或 `doc_id` 匹配（忽略大小写与多余空白）
- `小节标题` 是**规范化后**的标题（LLM 读到的就是它）
- 原始标题由 `.map.json` 反查，写进 frontmatter 的 `cites[].orig_heading`，
  深链用它做锚点——原始文件里存在的才是那个文本
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import frontmatter

CITE_RE = re.compile(r"【\[\[([^\[\]#]+)#([^\[\]]+)\]\]】")
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#([^\[\]|]+))?(?:\|[^\[\]]*)?\]\]")
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)

SECTIONS = [
    "定义与边界",
    "关键构成",
    "在本知识库中的证据",
    "与其他概念的关系",
    "争议与不同说法",
    "适用范围与局限",
    "来源索引",
]

REQUIRED_META = ("title", "slug", "type", "collection", "status",
                 "compiled_by", "compiled_at", "language", "sources")

VALID_TYPES = ("concept", "entity", "topic", "note")
VALID_STATUS = ("active", "stale", "disputed", "draft")


def norm_name(text: str) -> str:
    """名称归一：NFKC → 去空白 → 去反斜杠 → 小写。

    反斜杠来自 Markdown 转义（`### 4\. 高风险动作`），是显示痕迹不是语义，
    页面引用写 `4\.` 还是 `4.` 都应当视作同一个小节。
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("\\", "")
    return re.sub(r"\s+", "", t).lower()


@dataclass
class Cite:
    doc: str
    heading: str
    line: int


@dataclass
class Page:
    path: str
    slug: str
    meta: Dict
    body: str
    cites: List[Cite] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "")

    @property
    def type(self) -> str:
        return str(self.meta.get("type") or "")

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "active")

    @property
    def collection(self) -> str:
        return str(self.meta.get("collection") or "")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            (self.body + str(self.meta)).encode("utf-8")).hexdigest()

    def aliases(self) -> List[str]:
        raw = self.meta.get("aliases") or []
        return [str(a) for a in (raw if isinstance(raw, list) else [raw])]

    def source_docs(self) -> List[Dict]:
        return [s for s in (self.meta.get("sources") or []) if isinstance(s, dict)]


def parse_page(path: str) -> Page:
    text = open(path, encoding="utf-8").read()
    post = frontmatter.loads(text)
    meta = dict(post.metadata or {})
    body = post.content or ""

    cites = []
    for i, line in enumerate(body.split("\n"), 1):
        for m in CITE_RE.finditer(line):
            cites.append(Cite(doc=m.group(1).strip(), heading=m.group(2).strip(),
                              line=i))

    sections = [m.group(1).strip() for m in H2_RE.finditer(body)]
    return Page(path=path, slug=str(meta.get("slug") or ""), meta=meta,
                body=body, cites=cites, sections=sections)


def body_chars(body: str) -> int:
    """正文规模：去掉代码块与**引用/双链标记**后的字数。

    为什么不把引用算进去：这个上限的目的是逼页面「用链接下钻」，
    而引用标记本身恰恰是链接。把 `【[[文档标题#小节]]】` 计入等于在惩罚多给证据，
    方向反了。
    """
    text = re.sub(r"```.*?```", " ", body, flags=re.S)
    text = re.sub(r"【\[\[[^\]]*\]\]】", " ", text)
    text = re.sub(r"\[\[[^\]]*\]\]", " ", text)
    return len(re.sub(r"\s+", "", text))


def cjk_ratio(body: str, keep_en: Optional[List[str]] = None) -> Tuple[float, int, int]:
    """正文 CJK 占比。代码块、URL、行内代码不计；`keep_en` 里的专名不计拉丁字母。"""
    keep = {k.lower() for k in (keep_en or [])}
    text = body
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"!\[\[[^\]]*\]\]", " ", text)
    # 引用标记与双链是锚点，不是正文：英文文档名会成片拉低占比，先把它们去掉
    text = re.sub(r"【\[\[[^\]]*\]\]】", " ", text)
    text = re.sub(r"\[\[[^\]]*\]\]", " ", text)
    for w in keep:
        text = re.sub(re.escape(w), " ", text, flags=re.I)

    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = cjk + latin
    return (cjk / total if total else 0.0), cjk, latin
