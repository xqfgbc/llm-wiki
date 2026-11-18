"""把人审队列渲染成可勾选、可备注的 HTML 工作台。

页面是自包含的：数据内嵌，决策存 localStorage，导出 JSON 可直接回灌
`review human-apply`。模板在 `templates/human_review.html`，用 `__DATA__` 占位。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

DEFAULT_OUT = os.path.join("data", "review", "human_review.html")
TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "human_review.template.html")

COLLECTION_ORDER = ("tech-architecture", "agent-security", "ontology")
REASON_ORDER = (
    "duplicate_existing_page", "unresolved_blocker", "challenger_reject",
    "challenger_defer", "low_confidence", "thin_evidence",
    "acronym_unexpanded", "alias_hypernym", "type_conflict",
    "deferred_by_proposer", "missing_proposal", "missing_challenge",
)


def _order(c: str) -> int:
    return COLLECTION_ORDER.index(c) if c in COLLECTION_ORDER else 99


def _reason_order(r: str) -> int:
    return REASON_ORDER.index(r) if r in REASON_ORDER else 99


def _standalone(template: str) -> str:
    """把片段模板包成可直接双击打开的完整 HTML。"""
    if "</style>" not in template:
        return template
    head, body = template.split("</style>", 1)
    head += "</style>"
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        + head + "\n</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


def build(cfg, out_path: Optional[str] = None,
          collection: Optional[str] = None, standalone: bool = True) -> str:
    """读取 data/review/<collection>/_human_queue.json，写 HTML，返回路径。"""
    base = os.path.join(cfg.data_dir, "review")
    names = [collection] if collection else sorted(
        [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))],
        key=_order) if os.path.isdir(base) else []
    items: List[Dict] = []
    collections: List[str] = []
    reasons: List[str] = []

    for name in names:
        qpath = os.path.join(base, name, "_human_queue.json")
        if not os.path.exists(qpath):
            continue
        try:
            q = json.load(open(qpath, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if name not in collections:
            collections.append(name)
        for reason, rows in (q.get("groups") or {}).items():
            if reason not in reasons:
                reasons.append(reason)
            for r in rows:
                evidence = []
                for e in (r.get("evidence") or [])[:2]:
                    evidence.append({
                        "doc": str(e.get("doc") or ""),
                        "heading": str(e.get("heading") or ""),
                        "quote": str(e.get("quote") or "")[:320],
                    })
                objections = []
                for o in (r.get("objections") or [])[:3]:
                    objections.append({
                        "rule": str(o.get("rule") or ""),
                        "severity": str(o.get("severity") or ""),
                        "evidence": str(o.get("evidence") or "")[:320],
                        "fix": str(o.get("fix") or "")[:240],
                    })
                cid = str(r.get("candidate_id") or "")
                items.append({
                    # UI / localStorage 的唯一键：同一 candidate_id 可能出现在多个 collection
                    "id": "%s::%s" % (name, cid),
                    "candidate_id": cid,
                    "collection": name,
                    "reason": reason,
                    "surface": str(r.get("surface") or ""),
                    "action": str(r.get("action") or "defer"),
                    "canonical": str(r.get("canonical") or ""),
                    "type": str(r.get("type") or "entity"),
                    "aliases": [str(x) for x in (r.get("aliases") or [])],
                    "merge_into": str(r.get("merge_into") or ""),
                    "qualifier": str(r.get("qualifier") or ""),
                    "confidence": r.get("confidence") or 0,
                    "objections": objections,
                    "evidence": evidence,
                })

    collections = sorted(collections, key=_order)
    reasons = sorted(reasons, key=_reason_order)
    data = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": len(items),
        "collections": collections,
        "reasons": reasons,
        "items": items,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    template = open(TEMPLATE, encoding="utf-8").read()
    html = template.replace("__DATA__", payload)
    if standalone:
        html = _standalone(html)

    out_path = out_path or os.path.join(cfg.repo_root, DEFAULT_OUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
