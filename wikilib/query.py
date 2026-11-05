"""检索：BM25 over 源切片与 wiki 页。

`query` 子命令给 kg-qa 用，返回的 JSON 里 `wiki_pages` 是主通道，
`source_hits` 只作为补充证据。两者都来自同一个 `wiki.sqlite`。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from .index import BM25_WEIGHTS
from .tokenize import build_match_expr, query_terms

RELEVANCE_FLOOR = 0.30

SYNTHESIS_CUES = (
    "对比", "区别", "总结", "综述", "全景", "关系", "为什么", "怎么落地",
    "最佳实践", "有哪些", "演进", "如何", "攻击面", "治理", "架构",
)


def query_shape(query: str) -> str:
    """确定性判定查询形态，用来提示 kg-qa 先读原文还是先读 wiki。"""
    if len(query_terms(query)) >= 12:
        return "synthesis"
    return "synthesis" if any(c in (query or "") for c in SYNTHESIS_CUES) else "factoid"


def _bm25(conn, terms: List[str], limit: int, kind: str,
          collection: Optional[str]) -> List:
    expr = build_match_expr(terms)
    if not expr:
        return []
    where = "slides_fts MATCH ? AND d.kind = ?"
    args: list = [*BM25_WEIGHTS, expr, kind]
    if collection:
        where += " AND d.collection = ?"
        args.append(collection)
    sql = (
        "SELECT s.slide_id, s.doc_id, s.heading, s.heading_path, s.body, s.chars,"
        "       d.title, d.aliases, d.rel_path, d.type, d.status, d.cites_json, d.body AS page_body,"
        "       bm25(slides_fts, ?, ?, ?, ?, ?) AS score "
        "FROM slides_fts f "
        "JOIN slides s ON s.slide_id = f.rowid "
        "JOIN docs d   ON d.doc_id = s.doc_id "
        "WHERE " + where + " ORDER BY score LIMIT ?")
    args.append(limit)
    return conn.execute(sql, tuple(args)).fetchall()


def _group(rows, per_doc_bonus: float = 0.2) -> List[Dict]:
    """按文档聚合：页面是检索单位，不是切片。"""
    agg: Dict[str, Dict] = {}
    for r in rows:
        sim = -float(r["score"])
        g = agg.setdefault(r["doc_id"], {"sim": sim, "hits": [], "row": r})
        g["sim"] = max(g["sim"], sim)
        g["hits"].append(r)
    out = []
    for doc_id, g in agg.items():
        score = g["sim"] * (1 + per_doc_bonus * (len(g["hits"]) - 1))
        out.append({"doc_id": doc_id, "score": score, "hits": g["hits"], "row": g["row"]})
    out.sort(key=lambda x: -x["score"])
    return out


def _terms(query: str, glossary=None) -> List[str]:
    terms = query_terms(query)
    return glossary.expand(query, terms) if glossary is not None else terms


def _aliases_of(row) -> list:
    try:
        return json.loads(row["aliases"] or "[]")
    except (KeyError, json.JSONDecodeError, TypeError):
        return []


def search_pages(conn, query: str, top_k: int = 3,
                 collection: Optional[str] = None, glossary=None) -> List[Dict]:
    terms = _terms(query, glossary)
    if not terms:
        return []
    rows = _bm25(conn, terms, 40, "page", collection)
    grouped = _group(rows)
    if not grouped:
        return []
    # 相对下限：只有最佳分数的一定比例以内的页才算「相关」。
    # 没有这个下限时，一个语料里根本没有对应概念的问题（例如尚无页面的
    # confused deputy）也会拉回两三个弱相关页，反而污染上下文。
    floor = grouped[0]["score"] * RELEVANCE_FLOOR
    pages = []
    for item in [g for g in grouped if g["score"] >= floor][:top_k]:
        r = item["row"]
        try:
            cites = json.loads(r["cites_json"] or "[]")
        except json.JSONDecodeError:
            cites = []
        anchor_text = " ".join([r["title"] or ""] + _aliases_of(r) +
                               [h["heading"] or "" for h in item["hits"]])
        anchor_norm = anchor_text.lower()
        weak = not any(t and t.lower() in anchor_norm for t in terms)

        pages.append({
            "page": r["rel_path"],
            "title": r["title"],
            "weak": weak,
            "type": r["type"],
            "status": r["status"],
            "score": round(item["score"], 4),
            "summary": (item["hits"][0]["body"] or "")[:200],
            "body": r["page_body"] or "",
            "matched_sections": [h["heading"] for h in item["hits"]][:5],
            "cites": cites,
        })
    return pages


def search_sources(conn, query: str, top_k: int = 5,
                   collection: Optional[str] = None, glossary=None) -> List[Dict]:
    terms = _terms(query, glossary)
    if not terms:
        return []
    rows = _bm25(conn, terms, 40, "source", collection)
    out = []
    for item in _group(rows)[:top_k]:
        r = item["row"]
        out.append({
            "doc": r["title"],
            "doc_id": r["doc_id"],
            "rel_path": r["rel_path"],
            "heading": r["heading"],
            "heading_path": r["heading_path"],
            "body": (r["body"] or "")[:1500],
            "score": round(item["score"], 4),
        })
    return out


def run_query(conn, query: str, top_k: int = 3,
              collection: Optional[str] = None, glossary=None) -> Dict:
    terms = _terms(query, glossary)
    return {
        "query": query,
        "shape": query_shape(query),
        "expanded_terms": terms,
        "wiki_pages": search_pages(conn, query, top_k=top_k,
                                   collection=collection, glossary=glossary),
        "source_hits": search_sources(conn, query, top_k=5,
                                      collection=collection, glossary=glossary),
    }
