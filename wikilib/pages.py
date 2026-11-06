"""wiki 页的收集：给索引与 HTML 用。

`cites[].hash` 的口径
--------------------
页面 frontmatter 里记录的 `cites[].hash` 是**编译当时**的快照 hash。
索引时只在**缺失**时补填，绝不覆盖已有值——否则源文件一改，
索引顺手把 hash 刷新了，陈旧检测就永远查不出来。
"""
from __future__ import annotations

import glob
import os
import urllib.parse
from typing import Dict, List, Optional

from . import contracts
from .verify import Snapshot


def page_paths(pages_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(pages_dir, "**", "*.md"), recursive=True))


def collect(pages_dir: str, collection: str, snap: Snapshot,
            wiki_root: str, vault_root: str = "",
            chunk_hashes: Optional[Dict] = None) -> List[Dict]:
    """返回可直接喂给 `index.index_pages` 的列表。"""
    out = []
    for path in page_paths(pages_dir):
        name = os.path.basename(path)
        if name.upper() in ("CLAUDE.MD", "INDEX.MD", "LOG.MD"):
            continue
        page = contracts.parse_page(path)
        if not page.slug:
            continue

        cites = []
        for src in page.source_docs():
            ref = src.get("doc", "")
            doc_id, _, _ = snap.resolve(ref, "")
            doc_id = doc_id or ref
            abs_origin = snap.origins.get(doc_id, "")
            origin = ""
            if abs_origin and vault_root:
                origin = os.path.relpath(abs_origin, vault_root).replace(os.sep, "/")
            for c in (src.get("cites") or []):
                heading = str(c.get("heading", ""))
                heads = snap.docs.get(doc_id, {})
                key = contracts.norm_name(heading)
                # 空字符串表示「这是提升出来的伪标题，原文没有对应锚点」，
                # 不能回退成标题文本，否则深链会指向不存在的位置。
                orig_heading = heads[key] if key in heads else heading
                h = str(c.get("hash") or "")
                if not h and chunk_hashes:
                    h = chunk_hashes.get((doc_id, contracts.norm_name(heading)), "")
                cites.append({
                    "doc": doc_id,
                    "heading": heading,
                    "orig_heading": orig_heading,
                    "origin_path": origin,
                    "hash": h,
                })

        out.append({
            "slug": page.slug,
            "title": page.title,
            "type": page.type,
            "status": page.status,
            "aliases": page.aliases(),
            "cites": cites,
            "body": page.body,
            "rel_path": os.path.relpath(path, wiki_root).replace(os.sep, "/"),
        })
    return out


def deep_link(vault_name: str, vault_file: str, anchor: str = "") -> str:
    """Obsidian 深链。vault 名与文件路径都必须整段编码。"""
    url = "obsidian://open?vault=%s&file=%s" % (
        urllib.parse.quote(vault_name),
        urllib.parse.quote(vault_file, safe=""))
    if anchor:
        url += "#" + urllib.parse.quote(anchor, safe="")
    return url
