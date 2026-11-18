"""词汇表：概念译法、专有名词、缩写、保留英文。

为什么单独一层
--------------
`glossary` 只解决「中文提问 → 英文术语」的查询扩展；抽取 pipeline 还需要：
  * 专有名词的类型与别名（VPC 是 service，不是 concept）
  * 缩写到全称的映射（VPC → Virtual Private Cloud）
  * 已知劣质译法（写入前拦截）
所以把词表本身抽出来，`glossary` 退化为它的查询扩展视图。

文件格式
--------
沿用 vault 里的 `GLOSSARY.md`，按 `##` 分节：

  ## 概念        | 英文 | 中文 | 禁用译法 |
  ## 专有名词    | 名称 | 类型 | 中文/说明 | 别名 |
  ## 缩写        | 缩写 | 全称 | 中文 | 说明 |
  ## 保留英文    · 列表
  ## 已知劣质译法 · 列表

同一名称可以同时出现在「专有名词」和「缩写」里，加载时按 canonical 合并。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
_SEP_ROW = re.compile(r"^\|[\s:\-|]+\|$")
_LIST_SPLIT = re.compile(r"[·、,，]")

# section 前缀 -> 语义
SECTION_KINDS = (
    ("概念", "concept"),
    ("专有名词", "proper"),
    ("通用缩写", "common"),
    ("缩写", "acronym"),
    ("保留英文", "keep"),
    ("已知劣质译法", "banned"),
)

VALID_KINDS = ("concept", "proper", "acronym", "common")
KIND_LABEL = {"concept": "概念", "proper": "专有名词",
              "acronym": "缩写", "common": "通用缩写"}


def normalize_term(text: str) -> str:
    """去分隔符 + 小写。与 AI-KG 的 `kb/normalize.normalize_term` 同口径。"""
    return re.sub(r"[\s\-_·・]+", "", (text or "")).strip().lower()


@dataclass
class LexEntry:
    canonical: str
    kind: str                       # concept | proper | acronym
    zh: str = ""
    aliases: List[str] = field(default_factory=list)
    expansion: str = ""             # 缩写全称
    banned: List[str] = field(default_factory=list)
    note: str = ""

    def all_forms(self) -> List[str]:
        out = [self.canonical, self.zh, self.expansion] + list(self.aliases)
        return [x for x in dict.fromkeys(out) if x]

    def as_dict(self) -> Dict:
        return {
            "canonical": self.canonical,
            "kind": self.kind,
            "zh": self.zh,
            "aliases": list(self.aliases),
            "expansion": self.expansion,
            "banned": list(self.banned),
            "note": self.note,
        }


@dataclass
class Lexicon:
    entries: Dict[str, LexEntry] = field(default_factory=dict)   # key: normalize_term(canonical)
    keep_en: List[str] = field(default_factory=list)
    banned: List[str] = field(default_factory=list)
    path: str = ""

    # ---------------------------------------------------------------- 查询
    def find(self, surface: str) -> Optional[LexEntry]:
        key = normalize_term(surface)
        if not key:
            return None
        if key in self.entries:
            return self.entries[key]
        for e in self.entries.values():
            if any(normalize_term(f) == key for f in e.all_forms()):
                return e
        return None

    def expansion(self, surface: str) -> str:
        e = self.find(surface)
        if not e:
            return ""
        return e.expansion or (e.canonical if e.kind == "acronym" else "")

    def aliases_of(self, canonical: str) -> List[str]:
        e = self.find(canonical)
        if not e:
            return []
        return [x for x in e.all_forms() if normalize_term(x) != normalize_term(e.canonical)]

    def seed_terms(self) -> List[str]:
        """抽取阶段的种子词：所有 canonical / 别名 / 全称。

        `common`（通用缩写，如 API/HTTP）只用于展开与检索，不作为建页种子。
        """
        out: List[str] = []
        for e in self.entries.values():
            if e.kind == "common":
                continue
            out.extend(e.all_forms())
        return list(dict.fromkeys(x for x in out if x))

    def all_entries(self) -> List[LexEntry]:
        return sorted(self.entries.values(), key=lambda e: (e.kind, e.canonical.lower()))

    def check_acronyms(self, text: str) -> List[str]:
        """找出正文里出现、但词表没有登记的全大写缩写。"""
        known = set()
        for e in self.entries.values():
            for f in e.all_forms():
                known.add(normalize_term(f))
        out = []
        for m in re.finditer(r"\b[A-Z][A-Z0-9]{1,6}\b", text or ""):
            tok = m.group(0)
            if tok in ("I", "A", "OK", "ID", "UI", "OS"):
                continue
            if normalize_term(tok) not in known:
                out.append(tok)
        return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 解析
def _clean_cell(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _section_kind(line: str) -> Optional[str]:
    body = line.lstrip("#").strip()
    for prefix, kind in SECTION_KINDS:
        if body.startswith(prefix):
            return kind
    return None


def _ensure(lex: Lexicon, canonical: str, kind: str) -> LexEntry:
    key = normalize_term(canonical)
    # 先按「任意写法」找已有条目：专有名词里的别名 EKS 与缩写表里的 EKS
    # 必须归到同一条，否则 check 会报出假冲突。
    e = lex.find(canonical)
    if e is None:
        e = LexEntry(canonical=canonical, kind=kind)
        lex.entries[key] = e
    elif e.kind != kind:
        # 专有名词/缩写优先于概念：同一个词既是概念又有具体产品时，
        # 抽取阶段按「可指称的对象」处理更安全；概念信息仍然保留在 zh/banned 里。
        if kind in ("proper", "acronym") and e.kind in ("concept", "common"):
            e.kind = kind
        elif kind == "common" and e.kind == "concept":
            e.kind = kind
        elif kind in VALID_KINDS and e.kind not in VALID_KINDS:
            e.kind = kind
    return e


def _absorb(lex: Lexicon, e: LexEntry, other: LexEntry) -> None:
    """把另一条同义条目并入 e，并删除旧键。"""
    if other is e:
        return
    if not e.zh:
        e.zh = other.zh
    if not e.expansion:
        e.expansion = other.expansion
    for a in [other.canonical] + list(other.aliases) + ([other.expansion] if other.expansion else []):
        if a and normalize_term(a) != normalize_term(e.canonical) and a not in e.aliases:
            e.aliases.append(a)
    for b in other.banned:
        if b not in e.banned:
            e.banned.append(b)
    for k, v in list(lex.entries.items()):
        if v is other:
            del lex.entries[k]


def load(path: str) -> Lexicon:
    lex = Lexicon(path=path)
    if not path or not os.path.exists(path):
        return lex

    section: Optional[str] = None
    for raw in open(path, encoding="utf-8"):
        s = raw.strip()
        if s.startswith("##"):
            section = _section_kind(s)
            continue
        if not s:
            continue

        if section in ("keep", "banned"):
            for item in _LIST_SPLIT.split(s):
                item = item.strip()
                if not item:
                    continue
                (lex.keep_en if section == "keep" else lex.banned).append(item)
            continue

        if section not in VALID_KINDS:
            continue
        m = _TABLE_ROW.match(s)
        if not m or _SEP_ROW.match(s):
            continue
        cells = [_clean_cell(c) for c in m.group(1).split("|")]
        if not cells:
            continue
        first = cells[0].lower()
        if first in ("英文", "名称", "缩写", "---") or first.startswith("---"):
            continue

        if section == "concept":
            if len(cells) < 2 or not cells[0] or not cells[1]:
                continue
            e = _ensure(lex, cells[0], "concept")
            e.zh = e.zh or cells[1]
            if len(cells) >= 3:
                for bad in _LIST_SPLIT.split(cells[2]):
                    bad = bad.strip()
                    if bad and bad != "—":
                        e.banned.append(bad)
                        lex.banned.append(bad)

        elif section == "proper":
            if len(cells) < 2 or not cells[0]:
                continue
            e = _ensure(lex, cells[0], "proper")
            e.note = e.note or (cells[2] if len(cells) >= 3 else "")
            e.zh = e.zh or (cells[2] if len(cells) >= 3 else "")
            if len(cells) >= 4:
                for a in _LIST_SPLIT.split(cells[3]):
                    a = a.strip()
                    if a and a != "—" and normalize_term(a) != normalize_term(e.canonical):
                        if a not in e.aliases:
                            e.aliases.append(a)
                        other = lex.entries.get(normalize_term(a))
                        if other is not None and other is not e:
                            _absorb(lex, e, other)

        else:  # acronym / common
            if len(cells) < 2 or not cells[0]:
                continue
            e = _ensure(lex, cells[0], section)
            e.expansion = e.expansion or cells[1]
            if len(cells) >= 3 and cells[2] and cells[2] != "—":
                e.zh = e.zh or cells[2]
            if len(cells) >= 4 and cells[3] and cells[3] != "—":
                e.note = e.note or cells[3]

    lex.banned = list(dict.fromkeys(lex.banned))
    lex.keep_en = list(dict.fromkeys(lex.keep_en))
    return lex


# ---------------------------------------------------------------- 写回
def _row_for(e: LexEntry) -> str:
    def cell(x: str) -> str:
        return (x or "—").replace("|", "/")
    if e.kind == "concept":
        return "| %s | %s | %s |" % (
            cell(e.canonical), cell(e.zh), cell("、".join(e.banned)))
    if e.kind == "proper":
        return "| %s | %s | %s | %s |" % (
            cell(e.canonical), "proper", cell(e.zh or e.note),
            cell("、".join(e.aliases)))
    return "| %s | %s | %s | %s |" % (
        cell(e.canonical), cell(e.expansion), cell(e.zh), cell(e.note))


def _header_for(kind: str) -> Tuple[str, str]:
    if kind == "concept":
        return "## 概念", "| 英文 | 中文 | 禁用译法 |\n|---|---|---|"
    if kind == "proper":
        return "## 专有名词", "| 名称 | 类型 | 中文/说明 | 别名 |\n|---|---|---|---|"
    if kind == "common":
        return "## 通用缩写（不建页）", "| 缩写 | 全称 | 中文 | 说明 |\n|---|---|---|---|"
    return "## 缩写", "| 缩写 | 全称 | 中文 | 说明 |\n|---|---|---|---|"


def add_entry(path: str, *, canonical: str, kind: str, zh: str = "",
              aliases: Iterable[str] = (), expansion: str = "",
              note: str = "", banned: Iterable[str] = ()) -> LexEntry:
    """新增或更新一条词表条目，写回 Markdown。保留原有注释与其它条目。"""
    if kind not in VALID_KINDS:
        raise ValueError("未知词表类型：%s（可选：%s）" % (kind, ", ".join(VALID_KINDS)))
    lex = load(path)
    key = normalize_term(canonical)
    e = lex.entries.get(key) or LexEntry(canonical=canonical, kind=kind)
    e.kind = kind
    if zh:
        e.zh = zh
    if expansion:
        e.expansion = expansion
    if note:
        e.note = note
    for a in aliases:
        a = str(a).strip()
        if a and a not in e.aliases and normalize_term(a) != key:
            e.aliases.append(a)
    for b in banned:
        b = str(b).strip()
        if b and b not in e.banned:
            e.banned.append(b)
    lex.entries[key] = e

    _write_entry(path, e)
    return e


def _write_entry(path: str, e: LexEntry) -> None:
    lines: List[str] = []
    if os.path.exists(path):
        lines = open(path, encoding="utf-8").read().split("\n")
    else:
        lines = ["# 术语表", ""]

    section_title, header = _header_for(e.kind)
    target = e.kind
    prefix = section_title.lstrip("#").strip()
    sec_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("##") and line.lstrip("#").strip().startswith(prefix):
            sec_start = i
            break

    if sec_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_title, "", header, ""])
        sec_start = len(lines) - 3

    # 找该节结束位置
    sec_end = len(lines)
    for i in range(sec_start + 1, len(lines)):
        if lines[i].strip().startswith("##"):
            sec_end = i
            break

    # 找表头与最后一行表格
    header_idx = None
    last_row = None
    for i in range(sec_start + 1, sec_end):
        if lines[i].strip().startswith("|") and header_idx is None:
            header_idx = i
        if header_idx is not None and lines[i].strip().startswith("|"):
            last_row = i

    if header_idx is None:
        lines[sec_start + 1:sec_start + 1] = ["", header, ""]
        sec_end += 3
        header_idx = sec_start + 2
        last_row = header_idx + 1

    row = _row_for(e)
    clean = normalize_term(e.canonical)
    replaced = False
    for i in range(header_idx + 2, sec_end):
        m = _TABLE_ROW.match(lines[i].strip())
        if not m:
            continue
        cells = [_clean_cell(c) for c in m.group(1).split("|")]
        if cells and normalize_term(cells[0]) == clean:
            lines[i] = row
            replaced = True
            break
    if not replaced:
        lines.insert((last_row or header_idx + 1) + 1, row)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def missing_terms(path: str, text: str) -> List[str]:
    """列出正文里出现、但词表未登记的缩写。"""
    return load(path).check_acronyms(text)


def _term_pattern(term: str):
    parts = [p for p in re.split(r"[\s\-_·]+", term or "") if p]
    if not parts:
        return None
    body = r"[\s\-_·]+".join(re.escape(p) for p in parts)
    return re.compile(r"(?i)(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")


def provenance(path: str, term: str, raw_root: str) -> Dict[str, List[Dict]]:
    """跨 collection 查一个词的出处。

    词表是全局的，不按 collection 切开；每个词只要求「能在某篇源文档里找到」。
    返回 {collection: [{doc, heading, quote, offset}]}。
    """
    lex = load(path)
    entry = lex.find(term)
    forms = entry.all_forms() if entry else [term]
    forms = [f for f in dict.fromkeys(forms) if f]
    out: Dict[str, List[Dict]] = {}
    if not os.path.isdir(raw_root):
        return out
    for collection in sorted(os.listdir(raw_root)):
        cdir = os.path.join(raw_root, collection)
        if not os.path.isdir(cdir):
            continue
        for name in sorted(os.listdir(cdir)):
            if not name.endswith(".md"):
                continue
            fp = os.path.join(cdir, name)
            try:
                text = open(fp, encoding="utf-8").read()
            except OSError:
                continue
            try:
                import frontmatter as _fm
                post = _fm.loads(text)
                title = str((post.metadata or {}).get("title") or name[:-3])
                body = post.content or ""
            except Exception:
                title, body = name[:-3], text
            seen = set()
            for form in forms:
                pat = _term_pattern(form)
                if pat is None:
                    continue
                for m in pat.finditer(body):
                    line_start = body.rfind("\n", 0, m.start()) + 1
                    heading = ""
                    # 向上找最近的小节标题
                    for prev in reversed(body[:line_start].split("\n")):
                        hm = re.match(r"^#{1,6}\s+(.+?)\s*$", prev)
                        if hm:
                            heading = hm.group(1).strip()
                            break
                    key = (title, heading, m.start() // 80)
                    if key in seen:
                        continue
                    seen.add(key)
                    q = body[max(0, m.start() - 100):m.start() + 180]
                    q = re.sub(r"\s+", " ", q.replace("\n", " ")).strip()
                    out.setdefault(collection, []).append({
                        "doc": title,
                        "heading": heading or title,
                        "quote": q,
                        "offset": m.start(),
                    })
    return out
