r"""生成 `index.md` 与追加 `log.md`。

两条约定沿用社区实践：
- `index.md`：每页一行，按类型分组，用于**渐进披露**（先看目录，再点进去）
- `log.md`：append-only，标题固定为 `## [YYYY-MM-DD] <操作> | <对象>`，
  可以直接 `grep "^## \["` 出时间线
"""
from __future__ import annotations

import datetime
import os
from typing import Dict, List

from . import contracts

TYPE_LABEL = {"concept": "概念", "entity": "实体", "topic": "主题", "note": "笔记"}
STATUS_LABEL = {"active": "现行", "stale": "陈旧", "disputed": "有争议", "draft": "草稿"}


def _one_liner(page: contracts.Page) -> str:
    for line in page.body.split("\n"):
        s = line.strip()
        if s.startswith(">"):
            s = s.lstrip("> ").replace("**一句话**：", "").strip()
            s = s.replace("**", "")
            import re
            s = re.sub(r"【\[\[.*?\]\]】", "", s)
            return s[:120]
    return ""


def render_index(collection: str, label: str, pages: List[contracts.Page],
                 stats: Dict[str, int]) -> str:
    out = ["## %s（%d 页）" % (label, len(pages)), "",
           "共 %d 页 · 陈旧 %d · 有争议 %d" % (
               len(pages), stats.get("stale", 0), stats.get("disputed", 0)), ""]

    for typ in ("concept", "entity", "topic", "note"):
        group = [p for p in pages if p.type == typ]
        if not group:
            continue
        out.append("### %s（%d）" % (TYPE_LABEL.get(typ, typ), len(group)))
        out.append("")
        for p in sorted(group, key=lambda x: x.title):
            mark = "" if p.status == "active" else " · %s" % STATUS_LABEL.get(p.status, p.status)
            out.append("- [[%s]] — %s%s" % (p.title, _one_liner(p), mark))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def append_log(path: str, op: str, subject: str) -> None:
    """固定前缀，便于 `grep "^## \\["`。"""
    today = datetime.date.today().isoformat()
    line = "## [%s] %s | %s\n" % (today, op, subject)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 操作日志\n\n> append-only，由 `llm-wiki` 追加。\n\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def write_index(wiki_root: str, collection: str, label: str,
                pages: List[contracts.Page], stats: Dict[str, int]) -> str:
    """按 collection 分块写 `index.md`，多 collection 时互不覆盖。"""
    path = os.path.join(wiki_root, "index.md")
    block = render_index(collection, label, pages, stats)
    start = "<!-- llm-wiki:%s:start -->" % collection
    end = "<!-- llm-wiki:%s:end -->" % collection
    section = "%s\n%s\n%s" % (start, block.rstrip(), end)

    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if start in old and end in old:
        head, rest = old.split(start, 1)
        _, tail = rest.split(end, 1)
        content = head + section + tail
    else:
        if not old:
            old = "# Wiki 索引\n\n> 本文件由 `llm-wiki` 自动生成，请勿手工编辑。\n\n"
        content = old.rstrip() + "\n\n" + section + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
