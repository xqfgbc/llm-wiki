"""对抗复核的落盘与合并。

输入（由 LLM 按工单填写，均为 JSON 数组）：
  data/review/<collection>/proposals.json    proposer
  data/review/<collection>/challenges.json   challenger（盲审）
  data/review/<collection>/arbiters.json     arbiter（可选）

输出：
  data/review/<collection>/_resolved.json    每个候选的最终裁决
  data/review/<collection>/_human_queue.md   只交给人/下一轮 AI 的清单
  data/review/<collection>/_human_queue.json
  data/orders/<collection>/<slug>.json       建页/重编工单
  obsidian/wiki/entities.yaml                注册表

规则
----
* 只有 challenger 明确 uphold 且无 blocker，才直接采纳 proposer。
* 任何缺失、冲突、低置信、blobker 未解决 → 人审队列。
* 人审队列不是失败，是「AI 明确承认自己决定不了」的显式记录。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from . import contracts
from . import extract as extract_mod
from . import glossary as glossary_mod
from . import lexicon as lexicon_mod
from . import plan as plan_mod
from . import registry as registry_mod

RESOLVE_ACTIONS = ("accept_new", "merge_into", "split", "alias_only",
                   "reject", "defer")

HUMAN_REASONS = {
    "missing_proposal": "proposer 没有给出决定",
    "missing_challenge": "challenger 没有复核",
    "challenger_defer": "challenger 主动 defer",
    "unresolved_blocker": "存在未解决的 blocker 反对",
    "challenger_reject": "challenger 整体否决，无 arbiter 覆盖",
    "arbiter_low_confidence": "arbiter 置信度不足",
    "low_confidence": "最终置信度低于 0.7",
    "missing_merge_target": "merge_into 目标不存在",
    "thin_evidence": "concept 只有单篇来源",
    "acronym_unexpanded": "缩写未展开",
    "alias_hypernym": "别名是现有页的上位词，且未给 qualifier",
    "split_requires_review": "split 需要人确认拆分边界",
    "type_conflict": "同名存在不同类型",
    "decision_conflict": "proposer 与 challenger 结论不一致且无 arbiter",
    "duplicate_existing_page": "与现有页同名/别名冲突，需确认新建还是并入",
    "deferred_by_proposer": "proposer 主动 defer（证据不足或边界不清）",
}


# ---------------------------------------------------------------- I/O
def _load_json_list(path: str) -> List[Dict]:
    if not path or not os.path.exists(path):
        return []
    raw = open(path, encoding="utf-8").read().strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 兼容 LLM 常见的包装格式：{"proposals": [...]}
            for key in ("proposals", "challenges", "arbiters", "decisions",
                        "items", "candidates"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return []
    except json.JSONDecodeError:
        out = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except json.JSONDecodeError:
                continue
        return out


def load_candidates(path: str) -> Dict[str, Dict]:
    data = _load_json_list(path)
    if len(data) == 1 and isinstance(data[0], dict) and "candidates" in data[0]:
        data = data[0]["candidates"]
    return {str(c.get("candidate_id")): c for c in data if isinstance(c, dict)}


# ---------------------------------------------------------------- 裁决
@dataclass
class Resolution:
    candidate_id: str
    surface: str
    action: str = "defer"
    canonical: str = ""
    type: str = "entity"
    aliases: List[str] = field(default_factory=list)
    merge_into: str = ""
    qualifier: str = ""
    confidence: float = 0.0
    reviewer: str = ""
    objections: List[Dict] = field(default_factory=list)
    evidence: List[Dict] = field(default_factory=list)
    human_reason: str = ""
    human_options: List[str] = field(default_factory=list)

    @property
    def escalate(self) -> bool:
        return bool(self.human_reason)

    def as_dict(self) -> Dict:
        return {
            "candidate_id": self.candidate_id,
            "surface": self.surface,
            "action": self.action,
            "canonical": self.canonical,
            "type": self.type,
            "aliases": self.aliases,
            "merge_into": self.merge_into,
            "qualifier": self.qualifier,
            "confidence": self.confidence,
            "reviewer": self.reviewer,
            "objections": self.objections,
            "evidence": self.evidence,
            "human_reason": self.human_reason,
            "human_options": self.human_options,
        }


def _blockers(objections: Sequence[Dict]) -> List[Dict]:
    return [o for o in (objections or [])
            if str(o.get("severity") or "").lower() == "blocker"]


def _option_list(action: str, canonical: str, merge_into: str) -> List[str]:
    opts = []
    if action == "accept_new":
        opts.append("新建独立页：%s" % canonical)
    if merge_into:
        opts.append("并入现有页：%s" % merge_into)
    else:
        opts.append("并入某个现有页（需指定 slug）")
    opts.append("只作为别名，不建页")
    opts.append("拒绝（噪音/一次性名称）")
    opts.append("defer（补充证据后再判）")
    return opts


def resolve(proposals: List[Dict], challenges: List[Dict],
            arbiters: List[Dict], candidates: Dict[str, Dict],
            collection_docs: int = 99) -> List[Resolution]:
    prop = {str(p.get("candidate_id")): p for p in proposals if p.get("candidate_id")}
    chal = {str(c.get("candidate_id")): c for c in challenges if c.get("candidate_id")}
    arb = {str(a.get("candidate_id")): a for a in arbiters if a.get("candidate_id")}

    ids = list(dict.fromkeys(list(candidates) + list(prop) + list(chal) + list(arb)))
    out: List[Resolution] = []
    for cid in ids:
        c = candidates.get(cid, {})
        p = prop.get(cid, {})
        ch = chal.get(cid, {})
        a = arb.get(cid, {})
        surface = str(c.get("surface") or p.get("canonical") or cid)

        r = Resolution(candidate_id=cid, surface=surface,
                       evidence=c.get("evidence") or [])

        if not p:
            # covered / observation 本来就不该送复核，不能算“缺提案”。
            if str(c.get("gate") or "") in ("covered", "observation"):
                continue
            r.human_reason = "missing_proposal"
            r.action = "defer"
            r.human_options = _option_list("defer", surface, "")
            out.append(r)
            continue

        base = {
            "action": p.get("action") or "defer",
            "canonical": str(p.get("canonical") or surface),
            "type": str(p.get("type") or "entity"),
            "aliases": [str(x) for x in (p.get("aliases") or [])],
            "merge_into": str(p.get("merge_into") or ""),
            "qualifier": str(p.get("qualifier") or ""),
            "confidence": float(p.get("confidence") or 0.0),
        }

        objections = list(ch.get("objections") or [])
        verdict = str(ch.get("verdict") or "").lower()

        if not ch:
            # reject/defer 不产生页面，不值得花一轮对抗复核；直接采纳保守决定。
            # 只有「会改变 wiki 内容」的 accept/merge/split/alias 才强制盲审。
            if base["action"] in ("reject", "defer"):
                _fill(r, base, "proposer", objections)
                out.append(r)
                continue
            r.human_reason = "missing_challenge"
            _fill(r, base, "proposer", objections)
            r.human_options = _option_list(r.action, r.canonical, r.merge_into)
            out.append(r)
            continue

        if verdict == "defer":
            r.human_reason = "challenger_defer"
            _fill(r, base, "challenger", objections)
            r.human_options = _option_list(r.action, r.canonical, r.merge_into)
            out.append(r)
            continue

        if verdict == "revise":
            revised = ch.get("revised_proposal") or {}
            base = {
                "action": revised.get("action") or base["action"],
                "canonical": str(revised.get("canonical") or base["canonical"]),
                "type": str(revised.get("type") or base["type"]),
                "aliases": [str(x) for x in (revised.get("aliases") or base["aliases"])],
                "merge_into": str(revised.get("merge_into") or base["merge_into"]),
                "qualifier": str(revised.get("qualifier") or base["qualifier"]),
                "confidence": float(revised.get("confidence") or base["confidence"]),
            }

        if a:
            base = {
                "action": a.get("action") or base["action"],
                "canonical": str(a.get("canonical") or base["canonical"]),
                "type": str(a.get("type") or base["type"]),
                "aliases": [str(x) for x in (a.get("aliases") or base["aliases"])],
                "merge_into": str(a.get("merge_into") or base["merge_into"]),
                "qualifier": str(a.get("qualifier") or base["qualifier"]),
                "confidence": float(a.get("confidence") or base["confidence"]),
            }
            _fill(r, base, "arbiter", objections)
            if r.confidence < 0.7:
                r.human_reason = "arbiter_low_confidence"
        elif verdict == "reject" and not a:
            r.human_reason = "challenger_reject"
            _fill(r, base, "challenger", objections)
        elif _blockers(objections) and not a:
            r.human_reason = "unresolved_blocker"
            _fill(r, base, "challenger", objections)
        elif verdict in ("uphold", "revise", "") and not a:
            _fill(r, base, "challenger", objections)
            if r.confidence < 0.7:
                r.human_reason = "low_confidence"
        else:
            _fill(r, base, "proposer", objections)

        # 结构性校验：这些错误不能靠置信度掩盖
        if not r.human_reason:
            r.human_reason = _structural_problem(r, c, candidates, collection_docs)
        if r.human_reason:
            r.human_options = _option_list(r.action, r.canonical, r.merge_into)
        out.append(r)
    return out


def _fill(r: Resolution, base: Dict, reviewer: str, objections: List[Dict]) -> None:
    r.action = base["action"] if base["action"] in RESOLVE_ACTIONS else "defer"
    r.canonical = base["canonical"]
    r.type = base["type"] if base["type"] in registry_mod.VALID_TYPES else "entity"
    r.aliases = base["aliases"]
    r.merge_into = base["merge_into"]
    r.qualifier = base["qualifier"]
    r.confidence = base["confidence"]
    r.reviewer = reviewer
    r.objections = objections


def _structural_problem(r: Resolution, c: Dict, candidates: Dict[str, Dict],
                        collection_docs: int = 99) -> str:
    if r.action == "split":
        return "split_requires_review"
    if r.action == "merge_into" and not r.merge_into:
        return "missing_merge_target"
    if r.action == "accept_new":
        # 单文档 collection（如 ontology）不存在「第二篇来源」，不能用它卡概念页。
        if (r.type == "concept" and collection_docs >= 2
                and int(c.get("doc_coverage") or 0) < 2):
            return "thin_evidence"
        if "acronym_unexpanded" in str(c.get("gate_reason") or ""):
            forms = [r.canonical] + r.aliases
            if not any(" " in x or len(x) > len(r.canonical) + 2 for x in forms):
                return "acronym_unexpanded"
        if str(c.get("gate_reason") or "").startswith("alias_hypernym") and not r.qualifier:
            return "alias_hypernym"
    if r.action == "defer":
        return "low_confidence"
    return ""


# ---------------------------------------------------------------- 应用
def _acronym_of(canonical: str, aliases: List[str]) -> str:
    """只有短缩写才记 acronym_of；别名里的空格不代表它是缩写全称。"""
    import re as _re
    if not _re.fullmatch(r"[A-Z][A-Z0-9]{1,6}", canonical or ""):
        return ""
    return next((a for a in aliases if " " in a), "")


def _target_collection(r: Resolution, collection: str, global_mode: bool) -> str:
    """全局模式下，页面落到证据最多的那个 collection；否则保持原 collection。"""
    if not global_mode:
        return collection
    counts = Counter(str(e.get("collection") or "")
                     for e in r.evidence if e.get("collection"))
    return counts.most_common(1)[0][0] if counts else collection


def apply_resolutions(cfg, collection: str, resolutions: List[Resolution],
                      candidates: Dict[str, Dict], global_mode: bool = False) -> Dict:
    """写注册表、建页 order、人审队列。"""
    wiki_root = cfg.obsidian.wiki_root
    reg_path = os.path.join(wiki_root, "entities.yaml")
    reg = registry_mod.load(reg_path)

    page_slugs, page_titles, page_types = _existing_pages(
        wiki_root, collection, all_collections=global_mode)
    gl = glossary_mod.load_for(cfg)
    glossary_excerpt = "\n".join(
        "- %s = %s" % (zh, forms[1] if len(forms) > 1 else forms[0])
        for zh, forms in list(gl.forms.items())[:40])

    human: List[Resolution] = []
    orders_written: List[str] = []
    registry_changes: List[Dict] = []

    # 先登记本批所有 accept_new。否则 A merge_into B 时，如果 B 也在本批
    # 新建，_resolve_target 会找不到目标，整条链会被误判成人审。
    for r in resolutions:
        if r.escalate or r.action != "accept_new":
            continue
        target = _target_collection(r, collection, global_mode)
        reg.add(registry_mod.RegEntry(
            canonical=r.canonical, type=r.type, collection=target,
            collections=_collections_of(r, target),
            aliases=list(r.aliases), qualifier=r.qualifier,
            # 先以 deferred 占位，供同批 merge_into 找到目标；
            # 只有真正通过复核、落到 accept_new 分支时才置 active。
            status="deferred",
            slug=registry_mod.slugify(r.canonical),
            evidence=r.evidence[:3], decisions=[r.as_dict()]))

    for r in resolutions:
        if r.escalate:
            human.append(r)
            continue

        # 词表/注册表是全局的：某个实体已在别的 collection 被采纳为 active，
        # 这里的 defer/reject 只是重复候选，不应再进人审。
        existing = reg.find(r.canonical)
        if (existing is not None and existing.status == "active"
                and r.action in ("defer", "reject")):
            continue

        target_collection = _target_collection(r, collection, global_mode)
        entry = registry_mod.RegEntry(
            canonical=r.canonical,
            type=r.type,
            collection=target_collection,
            collections=_collections_of(r, target_collection),
            aliases=list(r.aliases),
            acronym_of=_acronym_of(r.canonical, r.aliases),
            qualifier=r.qualifier,
            slug=registry_mod.slugify(r.canonical),
            evidence=r.evidence[:5],
            decisions=[r.as_dict()],
        )

        if r.action == "reject":
            entry.status = "rejected"
            if not entry.evidence:
                entry.evidence = r.evidence[:2]
            reg.add(entry)
            registry_changes.append({"canonical": r.canonical, "status": "rejected"})
            continue

        if r.action == "alias_only":
            target = _resolve_target(r.merge_into, r.canonical, reg, page_slugs)
            if not target:
                r.human_reason = "missing_merge_target"
                human.append(r)
                continue
            reg.add(registry_mod.RegEntry(
                canonical=target, type=page_types.get(contracts.norm_name(target), r.type),
                collection=target_collection, collections=_collections_of(r, target_collection),
                aliases=[r.canonical] + r.aliases, status="active",
                slug=page_slugs.get(contracts.norm_name(target), ""),
                evidence=r.evidence[:3], decisions=[r.as_dict()]))
            registry_changes.append({"canonical": target, "alias_added": r.canonical})
            continue

        if r.action == "merge_into":
            target = _resolve_target(r.merge_into, r.canonical, reg, page_slugs)
            if not target:
                r.human_reason = "missing_merge_target"
                human.append(r)
                continue
            reg.add(registry_mod.RegEntry(
                canonical=target, type=page_types.get(contracts.norm_name(target), r.type),
                collection=target_collection, collections=_collections_of(r, target_collection),
                aliases=[r.canonical] + r.aliases, status="active",
                slug=page_slugs.get(contracts.norm_name(target), ""),
                evidence=r.evidence[:5], decisions=[r.as_dict()]))
            slug = page_slugs.get(contracts.norm_name(target)) or registry_mod.slugify(target)
            path = _write_page_order(cfg, target_collection, slug, target, r.type,
                                    r.evidence, glossary_excerpt,
                                    reason="合并候选：%s" % r.surface,
                                    candidate_id=r.candidate_id)
            if path:
                orders_written.append(path)
            registry_changes.append({"canonical": target, "merged": r.canonical})
            continue

        if r.action == "accept_new":
            # 最后一道确定性防重：候选的 canonical 或任一别名已指向现有页时，
            # 不允许再建一个同义页；交人确认「新建还是并入」。
            dup = next((page_slugs[contracts.norm_name(x)]
                        for x in [r.canonical] + r.aliases
                        if contracts.norm_name(x) in page_slugs), "")
            if dup:
                r.human_reason = "duplicate_existing_page"
                r.human_options = _option_list(r.action, r.canonical, dup)
                human.append(r)
                continue
            slug = _unique_slug(registry_mod.slugify(r.canonical), page_slugs)
            entry.slug = slug
            entry.status = "active"
            entry.status = "active"
            stored = reg.add(entry)
            stored.status = "active"
            stored.slug = slug
            path = _write_page_order(cfg, target_collection, slug, r.canonical, r.type,
                                    r.evidence, glossary_excerpt,
                                    reason="新实体：%s" % r.surface,
                                    candidate_id=r.candidate_id)
            if path:
                orders_written.append(path)
            registry_changes.append({"canonical": r.canonical, "status": "active",
                                     "slug": slug})
            continue

        # split / defer 或未知动作
        if r.action == "defer" and not r.human_reason:
            r.human_reason = "deferred_by_proposer"
            r.human_options = _option_list(r.action, r.canonical, r.merge_into)
        human.append(r)

    registry_mod.save(reg, reg_path)
    queue = _write_human_queue(cfg, collection, human, candidates)
    _write_json(os.path.join(cfg.data_dir, "review", collection, "_resolved.json"),
                [r.as_dict() for r in resolutions])
    return {
        "resolutions": len(resolutions),
        "human": len(human),
        "orders": len(orders_written),
        "registry_changes": len(registry_changes),
        "human_queue": queue,
        "order_paths": orders_written,
        "registry_path": reg_path,
    }


def _collections_of(r: Resolution, collection: str) -> List[str]:
    """证据来自哪个 collection 就记哪个；词表/注册表本身是全局的。"""
    out = {str(e.get("collection") or "") for e in r.evidence if e.get("collection")}
    if collection:
        out.add(collection)
    return sorted(x for x in out if x)


def _existing_pages(wiki_root: str, collection: str,
                    all_collections: bool = False):
    slugs, titles, types = {}, {}, {}
    roots = []
    if all_collections and os.path.isdir(wiki_root):
        for name in sorted(os.listdir(wiki_root)):
            path = os.path.join(wiki_root, name)
            if os.path.isdir(path):
                roots.append(path)
    else:
        roots = [os.path.join(wiki_root, collection)]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".md"):
                    continue
                p = contracts.parse_page(os.path.join(dirpath, name))
                if not p.slug:
                    continue
                keys = [contracts.norm_name(p.title), contracts.norm_name(p.slug)]
                keys += [contracts.norm_name(a) for a in p.aliases()]
                for key in keys:
                    if not key:
                        continue
                    slugs[key] = p.slug
                    titles[key] = p.title
                    types[key] = p.type
    return slugs, titles, types


def _resolve_target(merge_into: str, canonical: str,
                    reg: registry_mod.Registry,
                    page_slugs: Dict[str, str]) -> str:
    if not merge_into:
        return ""
    e = reg.find(merge_into)
    if e:
        return e.canonical
    key = contracts.norm_name(merge_into)
    if key in page_slugs:
        return merge_into
    return ""


def _unique_slug(slug: str, page_slugs: Dict[str, str]) -> str:
    used = set(page_slugs.values())
    if slug not in used:
        return slug
    n = 2
    while "%s-%d" % (slug, n) in used:
        n += 1
    return "%s-%d" % (slug, n)


EVIDENCE_LIMIT = 12
EVIDENCE_QUOTE_CHARS = 500

EVIDENCE_POLICY = {
    "purpose": "写页时优先使用这些证据片段；不要通读整篇源文档。",
    "rules": [
        "每条事实性断言必须来自 evidence[].quote；不得引入片段之外的事实。",
        "引用仍写 【[[doc#heading]]】；heading 必须与 evidence 中的完全一致。",
        "证据不足时在 定义与边界 / 适用范围与局限 中说明，不要补写。",
        "需要更多上下文时，只读 evidence 指向的 doc#heading，不读整篇。",
    ],
}


def _compact_evidence(evidence: List[Dict], collection: str,
                      limit: int = EVIDENCE_LIMIT) -> List[Dict]:
    """把候选证据压成 page order 里的证据包，控制体积、保留出处。"""
    out: List[Dict] = []
    seen = set()
    for e in evidence or []:
        doc = str(e.get("doc") or e.get("doc_id") or "")
        heading = str(e.get("heading") or "")
        quote = str(e.get("quote") or "")[:EVIDENCE_QUOTE_CHARS]
        if not doc or not heading:
            continue
        key = (doc, heading, quote)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "collection": str(e.get("collection") or collection),
            "doc": doc,
            "doc_id": str(e.get("doc_id") or doc),
            "heading": heading,
            "heading_path": str(e.get("heading_path") or heading),
            "quote": quote,
            "offset": e.get("offset"),
            "definitional": bool(e.get("definitional")),
            "in_code": bool(e.get("in_code")),
            "in_table": bool(e.get("in_table")),
        })
        if len(out) >= limit:
            break
    return out


def _write_page_order(cfg, collection: str, slug: str, title: str, typ: str,
                      evidence: List[Dict], glossary_excerpt: str,
                      reason: str, candidate_id: str = "") -> Optional[str]:
    if not evidence:
        return None
    grouped: Dict[str, List[Dict]] = {}
    for e in evidence:
        doc = str(e.get("doc") or e.get("doc_id") or "")
        if not doc:
            continue
        grouped.setdefault(doc, [])
        heading = str(e.get("heading") or "")
        if heading and all(h.get("norm") != heading for h in grouped[doc]):
            grouped[doc].append({"norm": heading, "orig": heading})

    sources = [{"doc_id": doc, "rel_path": "", "headings": heads}
               for doc, heads in grouped.items()]
    imp = plan_mod.Impact(page=slug, doc_id=sources[0]["doc_id"] if sources else "",
                          reason=reason, mode="new")
    order = plan_mod.build_order(slug, [imp], None, sources, glossary_excerpt)
    order["mode"] = "new"
    order["frontmatter"] = {
        "title": title,
        "slug": slug,
        "type": typ,
        "collection": collection,
        "status": "active",
    }
    order["entity_review"] = {
        "adversarial": True,
        "rule": "按 CLAUDE.md 七段结构撰写；每条断言带引用",
    }
    if candidate_id:
        order["candidate_id"] = candidate_id
    order["evidence"] = _compact_evidence(evidence, collection)
    order["evidence_policy"] = EVIDENCE_POLICY
    out_dir = os.path.join(cfg.orders_dir, collection)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.json" % slug.replace("/", "__"))
    if os.path.exists(path):
        old = json.load(open(path, encoding="utf-8"))
        old_sources = {s.get("doc_id"): s for s in old.get("sources") or []}
        for s in sources:
            old_sources.setdefault(s["doc_id"], s)
        order["sources"] = list(old_sources.values())
        order["reasons"] = list(dict.fromkeys(
            (old.get("reasons") or []) + order.get("reasons", [])))
        if old.get("evidence"):
            merged = list(old["evidence"]) + list(order["evidence"])
            order["evidence"] = _compact_evidence(merged, collection)
        if old.get("candidate_id"):
            order["candidate_id"] = old["candidate_id"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(order, f, ensure_ascii=False, indent=2)
    return path


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_human_queue(cfg, collection: str, human: List[Resolution],
                       candidates: Dict[str, Dict]) -> Dict:
    out_dir = os.path.join(cfg.data_dir, "review", collection)
    os.makedirs(out_dir, exist_ok=True)
    grouped: Dict[str, List[Resolution]] = {}
    for r in human:
        grouped.setdefault(r.human_reason or "unknown", []).append(r)

    lines = [
        "# 人审队列 · %s" % collection,
        "",
        "> 只收录 AI 明确无法收敛的候选。不要在别处补改；处理完请更新"
        " `decisions` 后重跑 `review apply`。",
        "",
        "共 %d 条。" % len(human),
        "",
    ]
    for reason, items in sorted(grouped.items(), key=lambda x: -len(x[1])):
        lines.append("## %s（%d）" % (HUMAN_REASONS.get(reason, reason), len(items)))
        lines.append("")
        for r in items:
            c = candidates.get(r.candidate_id, {})
            lines.append("### %s" % r.surface)
            lines.append("")
            lines.append("- candidate_id：`%s`" % r.candidate_id)
            lines.append("- 提案：`%s` → `%s`（%s，置信度 %.2f）"
                         % (r.action, r.canonical, r.type, r.confidence))
            if r.qualifier:
                lines.append("- 限定词：%s" % r.qualifier)
            lines.append("- 触发原因：%s" % HUMAN_REASONS.get(reason, reason))
            if r.objections:
                for o in r.objections[:3]:
                    lines.append("- 反对（%s/%s）：%s"
                                 % (o.get("rule", ""), o.get("severity", ""),
                                    o.get("evidence", "")[:160]))
            ev = (c.get("evidence") or [])[:2]
            for e in ev:
                lines.append("- 证据：%s # %s — %s"
                             % (e.get("doc", ""), e.get("heading", ""),
                                str(e.get("quote", ""))[:180]))
            lines.append("- 可选处理：%s" % "；".join(r.human_options or []))
            lines.append("")
    md = "\n".join(lines)
    with open(os.path.join(out_dir, "_human_queue.md"), "w", encoding="utf-8") as f:
        f.write(md)
    _write_json(os.path.join(out_dir, "_human_queue.json"), {
        "collection": collection,
        "count": len(human),
        "groups": {k: [r.as_dict() for r in v] for k, v in grouped.items()},
    })
    return {"path": os.path.join(out_dir, "_human_queue.md"),
            "count": len(human),
            "groups": {k: len(v) for k, v in grouped.items()}}


# ---------------------------------------------------------------- 清理过期 order
def _all_page_index(wiki_root: str):
    """slug -> collections，归一化名称 -> (slug, collection)。"""
    by_slug: Dict[str, Set[str]] = {}
    by_name: Dict[str, tuple] = {}
    if not os.path.isdir(wiki_root):
        return by_slug, by_name
    for collection in sorted(os.listdir(wiki_root)):
        root = os.path.join(wiki_root, collection)
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".md"):
                    continue
                p = contracts.parse_page(os.path.join(dirpath, name))
                if not p.slug:
                    continue
                by_slug.setdefault(p.slug, set()).add(collection)
                for n in [p.title, p.slug] + p.aliases():
                    key = contracts.norm_name(n)
                    if key:
                        by_name.setdefault(key, (p.slug, collection))
    return by_slug, by_name


def prune_orders(cfg, dry_run: bool = False) -> Dict:
    """把已失效的 `mode:new` order 移到 _stale，并降级无页面/order 的 active 条目。

    失效判定：
      * 注册表里没有该 slug，或状态不是 active；
      * 已有同名页面（任何 collection）；
      * 订单标题/别名命中了另一个页面（近重复）。
    """
    import glob as _glob
    import shutil as _shutil

    wiki_root = cfg.obsidian.wiki_root
    reg = registry_mod.load(os.path.join(wiki_root, "entities.yaml"))
    by_slug, by_name = _all_page_index(wiki_root)
    stale_dir = os.path.join(cfg.orders_dir, "_stale")
    removed: List[Dict] = []

    for path in sorted(_glob.glob(os.path.join(cfg.orders_dir, "*", "*.json"))):
        if os.sep + "_stale" + os.sep in path or os.sep + "extract" + os.sep in path:
            continue
        try:
            order = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if order.get("mode") != "new" or not order.get("entity_review"):
            continue
        slug = str(order.get("slug") or "")
        col = os.path.basename(os.path.dirname(path))
        fm = order.get("frontmatter") or {}
        title = str(fm.get("title") or "")

        # 先按 slug，再按标题/别名找注册表条目：历史上同一个实体可能
        # 先用全称建了 slug（json-web-token），后来订单用了缩写（jwt）。
        entry = next((e for e in reg.entries.values() if e.slug == slug), None)
        if entry is None:
            key = contracts.norm_name(title)
            entry = next((e for e in reg.entries.values()
                          if contracts.norm_name(e.canonical) == key
                          or any(contracts.norm_name(a) == key for a in e.aliases)), None)

        reason = ""
        if entry is None:
            reason = "page_exists_no_registry" if slug in by_slug else "registry_missing"
        elif entry.status != "active":
            reason = "registry_%s" % entry.status
        elif entry.slug in by_slug or slug in by_slug:
            reason = "page_exists"
        else:
            names = [title] + list(entry.aliases)
            for n in names:
                hit = by_name.get(contracts.norm_name(n))
                if hit and hit[0] not in (slug, entry.slug):
                    reason = "duplicate_of:%s/%s" % hit
                    break
        if not reason:
            # 同一实体、订单 slug 与注册表 slug 不一致：以订单 slug 为准
            if entry is not None and entry.slug != slug and not dry_run:
                entry.slug = slug
            continue
        dest = os.path.join(stale_dir, col, os.path.basename(path))
        removed.append({"order": path, "slug": slug, "reason": reason})
        if not dry_run:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _shutil.move(path, dest)

    # active 但没有页面/order 的条目降级
    live_orders = set()
    for path in _glob.glob(os.path.join(cfg.orders_dir, "*", "*.json")):
        if os.sep + "_stale" + os.sep in path or os.sep + "extract" + os.sep in path:
            continue
        try:
            o = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if o.get("slug"):
            live_orders.add(str(o["slug"]))
    deferred = []
    for e in reg.entries.values():
        if e.status == "active" and e.slug not in by_slug and e.slug not in live_orders:
            deferred.append(e.canonical)
            if not dry_run:
                e.status = "deferred"
    if not dry_run:
        registry_mod.save(reg, os.path.join(wiki_root, "entities.yaml"))
    return {"removed": removed, "deferred": deferred}


# ---------------------------------------------------------------- 证据包回填
def enrich_orders(cfg, collection: Optional[str] = None,
                  force: bool = False) -> Dict:
    """给已有的 `mode:new` page order 回填 evidence packet。

    证据来自 `_resolved.json`（优先）与 `_candidates.json`（兜底），
    按 candidate_id / canonical / slug / title 匹配。已有证据包默认跳过。
    """
    import glob as _glob

    cols = [collection] if collection else [c.name for c in cfg.collections]
    stats = {"orders": 0, "enriched": 0, "skipped": 0, "missing": 0}
    for col in cols:
        rpath = os.path.join(cfg.data_dir, "review", col, "_resolved.json")
        cpath = os.path.join(cfg.orders_dir, col, "extract", "_candidates.json")
        if not (os.path.exists(rpath) and os.path.exists(cpath)):
            continue
        try:
            resolved = json.load(open(rpath, encoding="utf-8"))
            cands = json.load(open(cpath, encoding="utf-8")).get("candidates") or []
        except (OSError, json.JSONDecodeError):
            continue
        by_cid = {str(c.get("candidate_id")): c for c in cands if isinstance(c, dict)}
        lookup: Dict[str, List[Dict]] = {}

        # 以全量候选为主：`_resolved.json` 只保留最近一次 apply 的条目，
        # 之前批次 resolutions 会丢；`_candidates.json` 才是全量证据来源。
        for c in cands:
            if not isinstance(c, dict):
                continue
            ev = c.get("evidence") or []
            if not ev:
                continue
            prop = c.get("proposed") or {}
            keys = [c.get("candidate_id"), c.get("surface"), c.get("key"),
                    prop.get("canonical")]
            keys += list(prop.get("aliases") or [])
            for k in keys:
                if not k:
                    continue
                nk = contracts.norm_name(str(k))
                if nk:
                    lookup.setdefault(nk, ev)
                lookup.setdefault(registry_mod.slugify(str(k)), ev)

        # resolved 的 evidence 优先级更高（可能经过修订）
        for r in resolved:
            if not isinstance(r, dict):
                continue
            ev = r.get("evidence") or []
            if not ev:
                c = by_cid.get(str(r.get("candidate_id") or ""))
                ev = (c or {}).get("evidence") or []
            if not ev:
                continue
            keys = [
                str(r.get("candidate_id") or ""),
                contracts.norm_name(str(r.get("canonical") or "")),
                contracts.norm_name(str(r.get("surface") or "")),
                registry_mod.slugify(str(r.get("canonical") or "")),
            ]
            for k in keys:
                if k:
                    lookup[k] = ev

        for path in sorted(_glob.glob(os.path.join(cfg.orders_dir, col, "*.json"))):
            if os.sep + "extract" + os.sep in path or os.sep + "_stale" + os.sep in path:
                continue
            try:
                order = json.load(open(path, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not order.get("entity_review") or order.get("mode") != "new":
                continue
            stats["orders"] += 1
            if order.get("evidence") and not force:
                stats["skipped"] += 1
                continue
            fm = order.get("frontmatter") or {}
            title = str(fm.get("title") or "")
            slug = str(order.get("slug") or "")
            cid = str(order.get("candidate_id") or "")
            ev = (lookup.get(cid) or lookup.get(contracts.norm_name(title))
                  or lookup.get(contracts.norm_name(slug)) or lookup.get(slug))
            if not ev:
                # 提案者改过 canonical（Approval → Human Approval），
                # 没法精确匹配；用标题/slug/reasons 里最长的候选词做包含匹配。
                hay = contracts.norm_name(
                    " ".join([title, slug] + [str(x) for x in (order.get("reasons") or [])]))
                best = ""
                for k, cand_ev in lookup.items():
                    nk = contracts.norm_name(k)
                    if len(nk) >= 5 and nk in hay and len(nk) > len(best):
                        best, ev = nk, cand_ev
            packet = _compact_evidence(ev or [], col)
            if not packet:
                stats["missing"] += 1
                continue

            order["evidence"] = packet
            order["evidence_policy"] = EVIDENCE_POLICY
            sources = order.get("sources") or []
            by_doc = {str(s.get("doc_id") or ""): s for s in sources}
            for e in packet:
                doc = e["doc"]
                s = by_doc.get(doc)
                if s is None:
                    s = {"doc_id": doc, "rel_path": "", "headings": []}
                    sources.append(s)
                    by_doc[doc] = s
                heads = s.setdefault("headings", [])
                if all(str(h.get("norm") or h.get("text") or "") != e["heading"]
                       for h in heads):
                    heads.append({"norm": e["heading"], "orig": e["heading"]})
            order["sources"] = sources
            with open(path, "w", encoding="utf-8") as f:
                json.dump(order, f, ensure_ascii=False, indent=2)
            stats["enriched"] += 1
    return stats


# ---------------------------------------------------------------- 预复核清单
def pending_report(cands: Iterable, limit: int = 200) -> List[Dict]:
    """在 LLM 复核之前，列出所有需要复核的候选（供技能逐条处理）。"""
    rows = []
    for c in cands:
        if getattr(c, "gate", "") != "needs_review":
            continue
        rows.append({
            "candidate_id": c.candidate_id,
            "surface": c.surface,
            "reason": c.gate_reason,
            "mentions": c.mentions,
            "doc_coverage": len(c.docs),
            "proposed": c.proposed,
        })
    rows.sort(key=lambda r: (-r["doc_coverage"], -r["mentions"]))
    return rows[:limit]
