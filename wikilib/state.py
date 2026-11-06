"""增量状态：源快照、页面依赖、运行记录。

状态库只是**缓存**。页面的真实依赖写在每页 frontmatter 的 `sources[].cites[]` 里，
所以删掉这个库也能从页面重建判断，只是要重算一遍哈希。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  collection   TEXT NOT NULL,
  doc_id       TEXT NOT NULL,
  rel_path     TEXT,
  source_sha256 TEXT,
  headings_json TEXT,
  normalized_at TEXT,
  PRIMARY KEY(collection, doc_id)
);

CREATE TABLE IF NOT EXISTS pages(
  collection   TEXT NOT NULL,
  slug         TEXT NOT NULL,
  path         TEXT,
  content_hash TEXT,
  status       TEXT,
  compiled_at  TEXT,
  PRIMARY KEY(collection, slug)
);

CREATE TABLE IF NOT EXISTS runs(
  run_id     TEXT PRIMARY KEY,
  collection TEXT,
  mode       TEXT,
  started    TEXT,
  finished   TEXT,
  stats_json TEXT
);
"""


@dataclass
class SourceState:
    doc_id: str
    rel_path: str
    sha256: str
    headings: List[Dict] = field(default_factory=list)

    def heading_keys(self) -> Dict[str, str]:
        return {h["norm"]: h["orig"] for h in self.headings}


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_sources(conn, collection: str) -> Dict[str, SourceState]:
    out = {}
    for r in conn.execute("SELECT * FROM sources WHERE collection=?", (collection,)):
        out[r["doc_id"]] = SourceState(
            doc_id=r["doc_id"], rel_path=r["rel_path"] or "",
            sha256=r["source_sha256"] or "",
            headings=json.loads(r["headings_json"] or "[]"))
    return out


def save_sources(conn, collection: str, states: Dict[str, SourceState]) -> None:
    conn.execute("DELETE FROM sources WHERE collection=?", (collection,))
    conn.executemany(
        "INSERT INTO sources(collection,doc_id,rel_path,source_sha256,headings_json,normalized_at)"
        " VALUES(?,?,?,?,?,?)",
        [(collection, s.doc_id, s.rel_path, s.sha256,
          json.dumps(s.headings, ensure_ascii=False),
          time.strftime("%Y-%m-%dT%H:%M:%S")) for s in states.values()])
    conn.commit()


def save_pages(conn, collection: str, pages: List[Dict]) -> None:
    conn.execute("DELETE FROM pages WHERE collection=?", (collection,))
    conn.executemany(
        "INSERT INTO pages(collection,slug,path,content_hash,status,compiled_at)"
        " VALUES(?,?,?,?,?,?)",
        [(collection, p["slug"], p.get("path", ""), p.get("content_hash", ""),
          p.get("status", "active"), p.get("compiled_at", "")) for p in pages])
    conn.commit()


def load_pages(conn, collection: str) -> Dict[str, str]:
    """slug -> content_hash，用于判断本次构建有哪些页发生了变化。"""
    return {r["slug"]: r["content_hash"] or ""
            for r in conn.execute(
                "SELECT slug, content_hash FROM pages WHERE collection=?",
                (collection,))}


def record_run(conn, collection: str, mode: str, started: float, stats: Dict) -> str:
    run_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), mode)
    conn.execute(
        "INSERT OR REPLACE INTO runs(run_id,collection,mode,started,finished,stats_json)"
        " VALUES(?,?,?,?,?,?)",
        (run_id, collection, mode,
         time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
         time.strftime("%Y-%m-%dT%H:%M:%S"),
         json.dumps(stats, ensure_ascii=False)))
    conn.commit()
    return run_id
