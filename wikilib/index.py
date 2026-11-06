"""建 `data/wiki.sqlite`：源切片与编译后的 wiki 页共用一套 FTS。

列序是契约：`slides_fts(doc_id, slide_id, heading, heading_path, body)` 对应
BM25 权重 `(0, 0, 10, 6, 1)`。改列序必须同步改 `BM25_WEIGHTS`，否则排序会静默错位。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, Iterable, List, Optional

from .chunk import Chunk
from .normalize import NormalizedDoc
from .tokenize import to_index_text

BM25_WEIGHTS = (0.0, 0.0, 10.0, 6.0, 1.0)

SCHEMA = """
CREATE TABLE docs(
  doc_id       TEXT PRIMARY KEY,
  collection   TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'source',   -- source | page
  rel_path     TEXT,
  title        TEXT NOT NULL,
  aliases      TEXT NOT NULL DEFAULT '[]',
  origin_path  TEXT,                             -- 源文档在 Obsidian 里的相对路径
  source_sha256 TEXT,
  type         TEXT,                             -- concept | entity | topic | note
  status       TEXT DEFAULT 'active',
  cites_json   TEXT DEFAULT '[]',
  body         TEXT,                             -- wiki 页的完整正文（源切片为空）
  updated_at   TEXT
);

CREATE TABLE slides(
  slide_id     INTEGER PRIMARY KEY,
  doc_id       TEXT NOT NULL,
  seq          INT,
  heading      TEXT,
  heading_orig TEXT,
  heading_path TEXT,
  body         TEXT,
  chars        INT,
  chunk_hash   TEXT,
  level        INT
);
CREATE INDEX idx_slides_doc ON slides(doc_id);

CREATE VIRTUAL TABLE slides_fts USING fts5(
  doc_id UNINDEXED,
  slide_id UNINDEXED,
  heading,
  heading_path,
  body,
  tokenize='unicode61'
);

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str, create: bool = False) -> sqlite3.Connection:
    if create:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
    elif not os.path.exists(path):
        raise FileNotFoundError("wiki 索引不存在：%s\n请先运行 `llm-wiki build`" % path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(SCHEMA)
    return conn


def build(conn: sqlite3.Connection, collection: str,
          docs: List[NormalizedDoc], chunks: Dict[str, List[Chunk]]) -> Dict[str, int]:
    """重建该 collection 的源切片（先删后插，保持与页面索引共存）。"""
    conn.execute("DELETE FROM slides_fts WHERE doc_id IN "
                 "(SELECT doc_id FROM docs WHERE collection=? AND kind='source')",
                 (collection,))
    conn.execute("DELETE FROM slides WHERE doc_id IN "
                 "(SELECT doc_id FROM docs WHERE collection=? AND kind='source')",
                 (collection,))
    conn.execute("DELETE FROM docs WHERE collection=? AND kind='source'", (collection,))

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    next_id = (conn.execute("SELECT COALESCE(MAX(slide_id),0) FROM slides").fetchone()[0] or 0) + 1

    n_slides = 0
    for doc in docs:
        conn.execute(
            "INSERT INTO docs(doc_id,collection,kind,rel_path,title,aliases,origin_path,"
            "source_sha256,type,status,cites_json,body,updated_at)"
            " VALUES(?,?,?,?,?,'[]',?,?,'source','active','[]',NULL,?)",
            (doc.doc_id, collection, "source", doc.rel_path, doc.title,
             doc.rel_path, doc.sha256, now))
        for ch in chunks.get(doc.doc_id, []):
            conn.execute(
                "INSERT INTO slides(slide_id,doc_id,seq,heading,heading_orig,heading_path,"
                "body,chars,chunk_hash,level) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (next_id, doc.doc_id, ch.ord, ch.index_heading, ch.heading_orig,
                 ch.heading_path, ch.body, ch.chars, ch.chunk_hash, ch.level))
            conn.execute(
                "INSERT INTO slides_fts(rowid,doc_id,slide_id,heading,heading_path,body)"
                " VALUES(?,?,?,?,?,?)",
                (next_id, doc.doc_id, next_id, to_index_text(ch.index_heading),
                 to_index_text(ch.heading_path), to_index_text(ch.body)))
            next_id += 1
            n_slides += 1

    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                 ("built_at:" + collection, now))
    conn.commit()
    return {"docs": len(docs), "slides": n_slides}


def index_pages(conn: sqlite3.Connection, collection: str,
                pages: Iterable[Dict]) -> int:
    """把编译好的 wiki 页写进同一套 FTS，供 `query` 命中。

    每页按 `##` 二级标题切块，页面整体正文存进 docs.body。
    """
    pages = list(pages)
    conn.execute("DELETE FROM slides_fts WHERE doc_id IN "
                 "(SELECT doc_id FROM docs WHERE collection=? AND kind='page')",
                 (collection,))
    conn.execute("DELETE FROM slides WHERE doc_id IN "
                 "(SELECT doc_id FROM docs WHERE collection=? AND kind='page')",
                 (collection,))
    conn.execute("DELETE FROM docs WHERE collection=? AND kind='page'", (collection,))

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    next_id = (conn.execute("SELECT COALESCE(MAX(slide_id),0) FROM slides").fetchone()[0] or 0) + 1
    n = 0

    for page in pages:
        doc_id = "page/%s/%s" % (collection, page["slug"])
        conn.execute(
            "INSERT INTO docs(doc_id,collection,kind,rel_path,title,aliases,origin_path,"
            "source_sha256,type,status,cites_json,body,updated_at)"
            " VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,?,?)",
            (doc_id, collection, "page", page.get("rel_path", ""), page["title"],
             json.dumps(page.get("aliases", []), ensure_ascii=False),
             page.get("type", "concept"), page.get("status", "active"),
             json.dumps(page.get("cites", []), ensure_ascii=False),
             page.get("body", ""), now))

        # 别名也要进索引：`Confused Deputy` 这类英文别名只存在 frontmatter 里，
        # 不进 FTS 的话，用别名搜不到对应页。
        alias_text = " ".join(page.get("aliases") or [])
        for seq, (heading, text) in enumerate(_split_page(page.get("body", ""))):
            path_text = "%s > %s" % (page["title"], heading)
            idx_path = "%s > %s > %s" % (page["title"], alias_text, heading)
            conn.execute(
                "INSERT INTO slides(slide_id,doc_id,seq,heading,heading_orig,heading_path,"
                "body,chars,chunk_hash,level) VALUES(?,?,?,?,?,?,?,?,NULL,2)",
                (next_id, doc_id, seq, heading, heading, path_text, text, len(text)))
            conn.execute(
                "INSERT INTO slides_fts(rowid,doc_id,slide_id,heading,heading_path,body)"
                " VALUES(?,?,?,?,?,?)",
                (next_id, doc_id, next_id, to_index_text(heading),
                 to_index_text(idx_path), to_index_text(text)))
            next_id += 1
            n += 1

    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                 ("pages_built_at:" + collection, now))
    conn.commit()
    return n


def _split_page(body: str) -> List[tuple]:
    """按 `##` 切页面正文；返回 [(heading, text)]。"""
    import re
    lines = body.split("\n")
    out, cur_heading, buf = [], "", []
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^##\s+(.+)$", line)
        if m:
            if buf and "".join(buf).strip():
                out.append((cur_heading, "\n".join(buf).strip()))
            cur_heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((cur_heading, "\n".join(buf).strip()))
    return out
