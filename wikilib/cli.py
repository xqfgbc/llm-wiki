"""命令行入口：`python -m wikilib.cli <command>`。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser

import yaml
from typing import List, Optional

from . import chunk as chunk_mod
from . import config as cfgmod
from . import exclusions as exclmod
from . import extract as extract_mod
from . import glossary as glossary_mod
from . import human_html
from . import index as index_mod
from . import lexicon as lexicon_mod
from . import registry as registry_mod
from . import review as review_mod
from . import normalize as norm_mod
from . import pages as pages_mod
from . import html as html_mod
from . import index_md as index_md_mod
from . import plan as plan_mod
from . import state as state_mod
from . import usage as usage_mod
from . import contracts
from . import query as query_mod
from . import verify as verify_mod


def _cfg(args) -> cfgmod.WikiConfig:
    return cfgmod.load(getattr(args, "config", None))


def _collection(cfg: cfgmod.WikiConfig, args):
    return cfg.collection(getattr(args, "collection", None))


def _run_all(args, fn) -> int:
    """不带 --collection 时对全部 collection 依次执行。"""
    cfg = _cfg(args)
    if getattr(args, "collection", None) or len(cfg.collections) <= 1:
        return -1                      # 调用方继续走单 collection 路径
    rc = 0
    for c in cfg.collections:
        args.collection = c.name
        rc = fn(args) or rc
    return rc


# ------------------------------------------------------------------ normalize
def cmd_normalize(args) -> int:
    _rc = _run_all(args, cmd_normalize)
    if _rc >= 0:
        return _rc
    cfg = _cfg(args)
    col = _collection(cfg, args)
    ex = exclmod.load(cfg.obsidian.wiki_root)
    report = norm_mod.scan_collection(col, cfg, ex)

    print("collection : %s（%s）" % (col.name, col.label))
    print("源目录     : %s" % ", ".join(col.sources))
    print("文档 %d · 排除 %d · 低置信标题 %d"
          % (len(report.docs), len(report.excluded), len(report.low_confidence)))
    print()

    for rel, kind, why in report.excluded:
        print("  ✗ 排除  %s\n          %s" % (rel, why))

    repaired = [(d.doc_id, h) for d in report.docs for h in d.headings if h.repaired]
    if repaired:
        print("\n标题断行修复（%d 处）" % len(repaired))
        for doc_id, h in repaired:
            print("  [%s]" % doc_id[:32])
            print("    原：%s" % h.orig)
            print("    新：%s" % h.norm)
            print()

    if report.low_confidence:
        print("低置信（不自动落盘，需人工判断，共 %d）" % len(report.low_confidence))
        for rel, heading, why in report.low_confidence:
            print("  · [%s] %s" % (rel[:30], heading[:70]))
        print()

    for w in report.warnings:
        print("  ⚠️  %s" % w)

    if args.dry_run:
        print("\n（--dry-run，未写盘）")
        return 0

    out = norm_mod.write_raw(report.docs, col.name, cfg)
    print("\n规范化快照 → %s" % out)
    return 0


# ------------------------------------------------------------------ build
def _prepare(cfg, col):
    ex = exclmod.load(cfg.obsidian.wiki_root)
    report = norm_mod.scan_collection(col, cfg, ex)
    norm_mod.write_raw(report.docs, col.name, cfg)
    chunks = chunk_mod.chunk_collection(report.docs, cfg)
    return report, chunks


def cmd_build(args) -> int:
    _rc = _run_all(args, cmd_build)
    if _rc >= 0:
        return _rc
    cfg = _cfg(args)
    col = _collection(cfg, args)
    report, chunks = _prepare(cfg, col)

    chunk_hashes = {}
    from . import contracts as _contracts
    for doc in report.docs:
        for ch in chunks.get(doc.doc_id, []):
            chunk_hashes[(doc.doc_id, _contracts.norm_name(ch.heading))] = ch.chunk_hash

    snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))
    page_dicts = pages_mod.collect(
        os.path.join(cfg.obsidian.wiki_root, col.name), col.name, snap,
        cfg.obsidian.wiki_root, cfg.obsidian.vault_root, chunk_hashes)

    conn = index_mod.connect(cfg.index_db, create=True)
    try:
        stats = index_mod.build(conn, col.name, report.docs, chunks)
        n_page_chunks = index_mod.index_pages(conn, col.name, page_dicts)
    finally:
        conn.close()

    total = sum(len(v) for v in chunks.values())
    state_conn = state_mod.connect(cfg.state_db)
    prev_hashes = state_mod.load_pages(state_conn, col.name)
    try:
        # ⚠️ 这里**不要**保存 sources 状态：那是 update 的职责。
        # build 只是重建索引；如果它顺手把源基线刷新了，先 build 再 update
        # 就永远看不到「新增文档」，新文档会一直进不了 wiki。
        state_mod.save_pages(state_conn, col.name, [
            {"slug": p.slug, "path": os.path.relpath(p.path, cfg.repo_root),
             "content_hash": p.content_hash, "status": p.status,
             "compiled_at": str(p.meta.get("compiled_at", ""))}
            for p in verify_mod.load_pages(os.path.join(cfg.obsidian.wiki_root, col.name))])
    finally:
        state_conn.close()

    page_objs = verify_mod.load_pages(os.path.join(cfg.obsidian.wiki_root, col.name))
    index_md_mod.write_index(cfg.obsidian.wiki_root, col.name, col.label, page_objs,
                             _status_counts(page_objs))

    _record_build_usage(cfg, col, page_objs, prev_hashes, report, chunks)

    print("collection : %s" % col.name)
    print("文档 %d · 源切片 %d · wiki 页 %d（%d 段）· 库 %s"
          % (stats["docs"], total, len(page_dicts), n_page_chunks,
             os.path.relpath(cfg.index_db, cfg.repo_root)))
    for rel, kind, why in report.excluded:
        print("  ✗ %s（%s）" % (rel, why[:40]))
    return 0


# ------------------------------------------------------------------ query
def cmd_query(args) -> int:
    cfg = _cfg(args)
    collection = getattr(args, "collection", None)
    try:
        conn = index_mod.connect(cfg.index_db)
        try:
            result = query_mod.run_query(conn, args.text, top_k=args.top_k,
                                         collection=collection,
                                         glossary=glossary_mod.load_for(cfg))
        finally:
            conn.close()
    except Exception as exc:                    # noqa: BLE001
        # kg-qa 会调这条命令；wiki 没建好绝不能把它拖垮。
        if args.strict:
            raise
        result = {"query": args.text, "shape": query_mod.query_shape(args.text),
                  "wiki_pages": [], "source_hits": [],
                  "error": "%s: %s" % (type(exc).__name__, exc)}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("# %s" % result["query"])
        print("形态：%s\n" % result["shape"])
        for p in result["wiki_pages"]:
            print("【%s】%s（%s）" % (p["type"], p["title"], p["status"]))
            print("  %s" % p["summary"].replace("\n", " ")[:160])
            for c in p["cites"][:3]:
                print("     ← %s # %s" % (c.get("doc", ""), c.get("heading", "")))
            print()
        if not result["wiki_pages"]:
            print("（没有命中的 wiki 页）\n")
        for s in result["source_hits"][:5]:
            print("· [%s] %s" % (s["doc"][:40], s["heading"]))
    return 0


def _emit_inventory(cfg, col, states):
    """为一个还没有页的 collection 产出页面清单任务。"""
    order = plan_mod.build_inventory(col.name, col.label, list(states.values()))
    path = plan_mod.write_orders([order], os.path.join(cfg.orders_dir, col.name))[0]
    print()
    print("页面清单任务 → %s" % os.path.relpath(path, cfg.repo_root))
    print("该 collection 还没有任何 wiki 页，需要先定页面划分。")
    print("由 Claude 读这份清单，提出页列表并建页，之后 update 才走增量。")


# ------------------------------------------------------------------ 内部
def _page_dicts(pages):
    out = []
    for p in pages:
        srcs = [{"doc": str(sg.get("doc", "")), "cites": sg.get("cites") or []}
                for sg in p.source_docs()]
        out.append({"slug": p.slug, "title": p.title, "aliases": p.aliases(),
                    "sources": srcs, "source_refs": [s["doc"] for s in srcs]})
    return out


def _states(report) -> dict:
    return {d.doc_id: state_mod.SourceState(
                doc_id=d.doc_id, rel_path=d.rel_path, sha256=d.sha256,
                headings=[{"norm": h.norm, "orig": h.orig,
                           "hash": h.content_hash} for h in d.headings])
            for d in report.docs}


def _write_index_and_log(cfg, col, pages, stats, events=()):
    index_md_mod.write_index(cfg.obsidian.wiki_root, col.name, col.label, pages, stats)
    log_path = os.path.join(cfg.obsidian.wiki_root, "log.md")
    for event in events:
        index_md_mod.append_log(log_path, event[0], event[1])


def _status_counts(pages):
    return {"stale": sum(1 for p in pages if p.status == "stale"),
            "disputed": sum(1 for p in pages if p.status == "disputed")}


# ------------------------------------------------------------------ update
def cmd_update(args) -> int:
    _rc = _run_all(args, cmd_update)
    if _rc >= 0:
        return _rc
    started = time.time()
    cfg = _cfg(args)
    col = _collection(cfg, args)
    ex = exclmod.load(cfg.obsidian.wiki_root)

    conn = state_mod.connect(cfg.state_db)
    try:
        previous = state_mod.load_sources(conn, col.name)
        report = norm_mod.scan_collection(col, cfg, ex)
        current = _states(report)

        diff = plan_mod.diff_sources(current, previous)
        pages = [c for c in verify_mod.load_pages(
            os.path.join(cfg.obsidian.wiki_root, col.name))]
        page_dicts = _page_dicts(pages)
        snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))

        # 首次运行只建立基线：没有历史状态时，把所有源都当成「新增」会
        # 把已有的 8 篇全部丢进候选池，制造一堆噪音。基线之后才谈增量。
        if not previous:
            state_mod.save_sources(conn, col.name, current)
            print("collection : %s" % col.name)
            print("建立基线：记录 %d 篇源文档的状态（本次不产出 work order）" % len(current))
            index_md_mod.write_index(cfg.obsidian.wiki_root, col.name, col.label,
                                     pages, _status_counts(pages))
            if not pages or args.inventory:
                _emit_inventory(cfg, col, current)
                return 0
            return 0

        if args.inventory:
            _emit_inventory(cfg, col, current)
            return 0


        print("collection : %s" % col.name)
        print("差异：新增 %d · 变更 %d · 删除 %d · 未变 %d"
              % (len(diff.added), len(diff.modified), len(diff.deleted),
                 len(diff.unchanged)))

        if diff.empty and not args.force:
            print("没有变更，无需重编。")
            index_md_mod.write_index(cfg.obsidian.wiki_root, col.name, col.label,
                                     pages, _status_counts(pages))
            return 0

        norm_mod.write_raw(report.docs, col.name, cfg)
        snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))

        impacts = plan_mod.impact(diff, page_dicts, snap.titles, current, previous)
        by_page = {}
        for imp in impacts:
            by_page.setdefault(imp.page, []).append(imp)

        gl = glossary_mod.load_for(cfg)
        excerpt = "\n".join("- %s = %s" % (zh, forms[1]) for zh, forms in
                             list(gl.forms.items())[:40])
        orders = []
        unmatched = []
        for slug, imps in by_page.items():
            if slug == "(候选)":
                unmatched.extend(imps)
                continue
            path = next((p.path for p in pages if p.slug == slug), None)
            existing = open(path, encoding="utf-8").read() if path else ""
            srcs = []
            for imp in imps:
                st = current.get(imp.doc_id)
                if st:
                    heads = st.headings
                    if getattr(imp, "headings", None):
                        wanted = {contracts.norm_name(h) for h in imp.headings}
                        picked = [h for h in st.headings
                                  if contracts.norm_name(h.get("norm", "")) in wanted]
                        if picked:
                            heads = picked
                    srcs.append({"doc_id": st.doc_id, "rel_path": st.rel_path,
                                 "headings": heads})
            order = plan_mod.build_order(slug, imps, existing, srcs, excerpt)
            if not getattr(args, "no_evidence", False):
                plan_mod.attach_evidence(
                    order, os.path.join(cfg.raw_dir, col.name), existing)
            orders.append(order)

        paths = plan_mod.write_orders(orders, os.path.join(cfg.orders_dir, col.name))
        if unmatched:
            # 新增文档没有命中任何已有页时，过去会静默丢弃（`(候选)` 直接 continue）。
            # 现在显式落盘，交给 extract 做候选抽取与人审，不再丢失素材。
            _write_json(os.path.join(cfg.orders_dir, col.name, "_unmatched.json"), {
                "collection": col.name,
                "note": "新文档未命中任何已有页；请运行 extract 生成候选",
                "items": [{"doc_id": i.doc_id, "reason": i.reason}
                          for i in unmatched],
            })
            print("未命中已有页的新文档 %d 篇 → %s"
                  % (len(unmatched), os.path.relpath(
                      os.path.join(cfg.orders_dir, col.name, "_unmatched.json"),
                      cfg.repo_root)))
        order_text = "\n".join(open(x, encoding="utf-8").read() for x in paths)
        if orders:
            usage_mod.record(cfg, usage_mod.new_record(
                "update", col.name,
                subject="产出 %d 份 work order" % len(orders),
                pages=[o["slug"] for o in orders],
                chars_in=len(order_text),
                est_in=usage_mod.estimate_tokens(order_text),
                note="输入=work order 正文；撰写阶段的实际消耗需在下一次 build 或 import 时统计",
            ))
        state_mod.save_sources(conn, col.name, current)
        state_mod.save_pages(conn, col.name, [
            {"slug": p.slug, "path": os.path.relpath(p.path, cfg.repo_root),
             "content_hash": p.content_hash, "status": p.status,
             "compiled_at": str(p.meta.get("compiled_at", ""))} for p in pages])
        state_mod.record_run(conn, col.name, "update", started,
                             {"diff": diff.as_dict(), "orders": len(orders)})
    finally:
        conn.close()

    for imp in impacts:
        print("  · %s ← %s（%s）" % (imp.page, imp.doc_id[:44], imp.reason[:60]))
    print()
    print("work order %d 份 → %s" % (len(orders), os.path.relpath(
        os.path.join(cfg.orders_dir, col.name), cfg.repo_root)))
    print("下一步：让 Claude 按 order 重写页面，再跑 `build` 与 `lint`。")

    stats = _status_counts(pages)
    _write_index_and_log(cfg, col, pages, stats,
                         events=[("update", "新增 %d / 变更 %d / 删除 %d"
                                  % (len(diff.added), len(diff.modified), len(diff.deleted)))])
    return 0


def _record_build_usage(cfg, col, page_objs, prev_hashes, report, chunks):
    """记录本次构建相对上次的增量：改了哪些页、读了多少来源、产出多少字。

    输入按「被引用的切片正文」计，输出按「改动的页面正文」计。
    两边都按中英文字符分别折算，避免把英文来源按中文系数算高一倍。
    """
    changed = [p for p in page_objs if prev_hashes.get(p.slug) != p.content_hash]
    if not changed:
        return

    body_of = {}
    for doc in report.docs:
        for ch in chunks.get(doc.doc_id, []):
            body_of[(doc.doc_id, contracts.norm_name(ch.heading))] = ch.body

    in_text, out_text = [], []
    for page in changed:
        for src in page.source_docs():
            doc_id = src.get("doc", "")
            for c in (src.get("cites") or []):
                body = body_of.get((doc_id, contracts.norm_name(str(c.get("heading", "")))))
                if body:
                    in_text.append(body)
        out_text.append(page.body)

    t_in, t_out = "\n".join(in_text), "\n".join(out_text)
    usage_mod.record(cfg, usage_mod.new_record(
        "build", col.name,
        subject="编译/更新 %d 页" % len(changed),
        pages=[p.slug for p in changed],
        chars_in=len(t_in), chars_out=len(t_out),
        est_in=usage_mod.estimate_tokens(t_in),
        est_out=usage_mod.estimate_tokens(t_out),
        note="输入=被引用的来源切片，输出=改动的页面正文；均为估算",
    ))


# ------------------------------------------------------------------ stamp
def cmd_stamp(args) -> int:
    _rc = _run_all(args, cmd_stamp)
    if _rc >= 0:
        return _rc
    """把当前快照的内容 hash 写进页面 frontmatter，作为后续陈旧判据。"""
    import frontmatter
    cfg = _cfg(args)
    col = _collection(cfg, args)
    snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))
    pages_dir = os.path.join(cfg.obsidian.wiki_root, col.name)

    stamped = missing = 0
    for path in pages_mod.page_paths(pages_dir):
        if os.path.basename(path).upper() in ("CLAUDE.MD", "INDEX.MD", "LOG.MD"):
            continue
        post = frontmatter.load(path)
        changed = False
        for src in (post.metadata.get("sources") or []):
            ref = str(src.get("doc", ""))
            doc_id, _, _ = snap.resolve(ref, "")
            doc_id = doc_id or ref
            for c in (src.get("cites") or []):
                key = contracts.norm_name(str(c.get("heading", "")))
                cur = snap.hashes.get(doc_id, {}).get(key, "")
                if not cur:
                    missing += 1
                    continue
                if args.force or not c.get("hash"):
                    c["hash"] = cur
                    changed = True
                    stamped += 1
        if changed:
            text = frontmatter.dumps(post) + "\n"
            # YAML 1.1 会把 04214150 这类以 0 开头的 hash 当八进制整数，
            # 回读后变成 1120360，陈旧检测就会误报。统一加引号。
            text = re.sub(
                r"^(\s*(?:-\s*)?hash:\s*)([0-9a-fA-F]{8})(\s*)$",
                r"\1'\2'\3", text, flags=re.M)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    print("已写入 hash %d 条 · 找不到对应小节的 %d 条" % (stamped, missing))
    return 0


# ------------------------------------------------------------------ init
def cmd_init(args) -> int:
    """把 schema / 术语表 / 排除名单模板部署到 wiki 根，并建好 collection 目录。

    只写**不存在**的文件，绝不覆盖已有内容。
    """
    cfg = _cfg(args)
    root = cfg.obsidian.wiki_root
    tpl = os.path.join(cfg.repo_root, "templates")
    os.makedirs(root, exist_ok=True)

    created, skipped = [], []
    for name in ("CLAUDE.md", "GLOSSARY.md", "EXCLUSIONS.yaml"):
        src, dst = os.path.join(tpl, name), os.path.join(root, name)
        if os.path.exists(dst):
            skipped.append(name)
            continue
        with open(src, encoding="utf-8") as f:
            content = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        created.append(name)

    for col in cfg.collections:
        for sub in ("concepts", "entities", "topics", "notes"):
            os.makedirs(os.path.join(root, col.name, sub), exist_ok=True)

    print("wiki 根：%s" % root)
    print("新建：%s" % (", ".join(created) or "无"))
    print("已存在跳过：%s" % (", ".join(skipped) or "无"))
    print("目录已就绪：%s" % ", ".join(c.name for c in cfg.collections))
    return 0


# ------------------------------------------------------------------ doctor
def cmd_doctor(args) -> int:
    """对真实数据做一致性体检：索引、快照、页面三者是否对得上。"""
    cfg = _cfg(args)
    problems = []
    notes = []

    # 1 配置里的 collection 是否都有索引与快照
    names = {c.name for c in cfg.collections}
    if os.path.exists(cfg.index_db):
        conn = index_mod.connect(cfg.index_db)
        try:
            db_cols = {r[0] for r in conn.execute(
                "SELECT DISTINCT collection FROM docs")}
            for col in sorted(names - db_cols):
                problems.append("collection 未索引：%s" % col)
            for col in sorted(db_cols - names):
                problems.append("索引里有已删除的 collection：%s" % col)

            for col in sorted(names & db_cols):
                n_idx = conn.execute(
                    "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='source'",
                    (col,)).fetchone()[0]
                n_raw = len([f for f in os.listdir(os.path.join(cfg.raw_dir, col))
                             if f.endswith(".md")]) if os.path.isdir(
                    os.path.join(cfg.raw_dir, col)) else 0
                if n_idx != n_raw:
                    problems.append("%s：索引 %d 篇，快照 %d 篇" % (col, n_idx, n_raw))
                n_page_idx = conn.execute(
                    "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='page'",
                    (col,)).fetchone()[0]
                pdir = os.path.join(cfg.obsidian.wiki_root, col)
                n_page_file = len([p for p in pages_mod.page_paths(pdir)
                                   if os.path.basename(p).upper()
                                   not in ("CLAUDE.MD", "INDEX.MD", "LOG.MD")
                                   ]) if os.path.isdir(pdir) else 0
                if n_page_idx != n_page_file:
                    problems.append("%s：索引 %d 页，磁盘 %d 页"
                                    % (col, n_page_idx, n_page_file))
        finally:
            conn.close()
    else:
        problems.append("索引不存在：%s" % cfg.index_db)

    # 2 页面自身
    for col in sorted(names):
        pdir = os.path.join(cfg.obsidian.wiki_root, col)
        if not os.path.isdir(pdir):
            notes.append("%s 还没有 wiki 目录（正常，尚未建页）" % col)
            continue
        for path in pages_mod.page_paths(pdir):
            page = contracts.parse_page(path)
            if page.type not in contracts.VALID_TYPES:
                problems.append("%s：type 非法 %r" % (path, page.meta.get("type")))
            if page.meta.get("collection") != col:
                problems.append("%s：frontmatter.collection=%r，实际目录是 %s"
                                % (path, page.meta.get("collection"), col))

    # 3 快照目录没有对应 collection
    if os.path.isdir(cfg.raw_dir):
        for d in os.listdir(cfg.raw_dir):
            if d.startswith("."):
                continue
            if d not in names:
                notes.append("快照目录无对应 collection：raw/%s（可删）" % d)

    print("collection %d 个 · 问题 %d · 提示 %d" % (len(names), len(problems), len(notes)))
    for p in problems:
        print("  ✗ %s" % p)
    for n in notes:
        print("  · %s" % n)
    return 1 if problems else 0


# ------------------------------------------------------------------ note
NOTE_SKELETON = """# {title}

> **一句话**：（待补）

## 背景

## 结论

## 关联

{links}
"""


def _slugify(text: str) -> str:
    """标题 → slug。中文标题没有可靠的音译，退回时间戳式命名。"""
    import re as _re
    ascii_part = _re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if ascii_part and len(ascii_part) >= 3 and not _re.search(r"[\u4e00-\u9fff]", text):
        return ascii_part[:60]
    return "note-%s" % time.strftime("%Y%m%d-%H%M")


def cmd_note(args) -> int:
    """把一次对话的结论或人工判断落成笔记页。"""
    cfg = _cfg(args)
    col = _collection(cfg, args)
    title = args.title.strip()
    slug = args.slug or _slugify(title)

    out_dir = os.path.join(cfg.obsidian.wiki_root, col.name, "notes")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, slug + ".md")
    if os.path.exists(path) and not args.force:
        print("已存在：%s\n用 --force 覆盖，或换一个 --slug" % path, file=sys.stderr)
        return 1

    if args.body:
        if not os.path.exists(args.body):
            print("正文文件不存在：%s" % args.body, file=sys.stderr)
            return 1
        body = open(args.body, encoding="utf-8").read().strip()
        if not body.startswith("# "):
            body = "# %s\n\n%s" % (title, body)
    else:
        links = "\n".join("- [[%s]]" % x.strip()
                          for x in (args.link or "").split(",") if x.strip())
        body = NOTE_SKELETON.format(title=title, links=links or "（待补）")

    meta = {
        "title": title, "slug": slug, "type": "note", "collection": col.name,
        "status": args.status, "origin": args.origin,
        "compiled_by": "human", "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "language": "zh", "sources": [],
    }
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n%s\n---\n\n%s\n" % (head, body))

    index_md_mod.append_log(
        os.path.join(cfg.obsidian.wiki_root, "log.md"), "note", title)
    print("已创建笔记：%s" % path)
    print("origin=%s · status=%s" % (args.origin, args.status))
    print("下一步：\n  build   # 让笔记进入检索\n  lint")
    return 0


# ------------------------------------------------------------------ collection
def _local_path(args) -> str:
    return getattr(args, "config", None) or cfgmod.LOCAL_CONFIG


def cmd_collection(args) -> int:
    import yaml
    path = _local_path(args)

    if args.action == "list":
        cfg = cfgmod.load(path, strict_sources=False)
        if getattr(args, "json", False):
            out = []
            for c in cfg.collections:
                item = {"name": c.name, "label": c.label, "sources": c.sources,
                        "docs": 0, "pages": 0}
                if os.path.exists(cfg.index_db):
                    conn = index_mod.connect(cfg.index_db)
                    try:
                        item["docs"] = conn.execute(
                            "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='source'",
                            (c.name,)).fetchone()[0]
                        item["pages"] = conn.execute(
                            "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='page'",
                            (c.name,)).fetchone()[0]
                    finally:
                        conn.close()
                out.append(item)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        for c in cfg.collections:
            print("%-16s %-14s %s" % (c.name, c.label, ", ".join(c.sources)))
            if os.path.exists(cfg.index_db):
                conn = index_mod.connect(cfg.index_db)
                try:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='source'",
                        (c.name,)).fetchone()[0]
                    m = conn.execute(
                        "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='page'",
                        (c.name,)).fetchone()[0]
                    print("%-16s 源文档 %d · wiki 页 %d" % ("", n, m))
                finally:
                    conn.close()
        return 0

    raw = {}
    if os.path.exists(path):
        raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
    cols = raw.setdefault("collections", [])

    if args.action == "add":
        if not args.name or not args.source:
            print("add 需要 --name 与 --source", file=sys.stderr)
            return 2
        if any(c.get("name") == args.name for c in cols):
            print("collection 已存在：%s" % args.name, file=sys.stderr)
            return 1
        # 只接受 Obsidian vault 内的子目录：事实源必须唯一
        try:
            src = cfgmod.validate_source_path(
                cfgmod.load(path, strict_sources=False), args.source)
        except cfgmod.ConfigError as exc:
            print("源目录不合规：%s" % exc, file=sys.stderr)
            return 1
        for c in cols:
            if src in [os.path.realpath(x) for x in (c.get("sources") or [])]:
                print("该路径已登记为 collection「%s」，无需重复添加" % c.get("name"))
                return 0
        cols.append({"name": args.name,
                     "label": args.label or args.name,
                     "sources": [src]})
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        print("已添加 collection：%s → %s" % (args.name, src))
        print("下一步：\n  build --collection %s    # 建索引\n"
              "  update --collection %s   # 建立基线" % (args.name, args.name))
        return 0

    if args.action == "remove":
        kept = [c for c in cols if c.get("name") != args.name]
        if len(kept) == len(cols):
            print("没有这个 collection：%s" % args.name, file=sys.stderr)
            return 1
        raw["collections"] = kept
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        print("已从配置移除：%s" % args.name)
        print("注意：已生成的 wiki 页与索引不会自动删除，需要手工清理。")
        return 0

    return 2


# ------------------------------------------------------------------ usage
def _latest_project_dir():
    """在 ~/.claude/projects 下找最近被写入的会话目录。"""
    import glob
    cands = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    if not cands:
        return None
    return os.path.dirname(max(cands, key=os.path.getmtime))


def cmd_usage(args) -> int:
    cfg = _cfg(args)
    if args.import_dir or args.session or args.import_now:
        project_dir = os.path.expanduser(args.import_dir) if args.import_dir \
            else _latest_project_dir()
        if not project_dir:
            print("没找到任何会话记录")
            return 1
        res = usage_mod.import_session(cfg, project_dir, args.session,
                                       kind=args.kind,
                                       checkpoint=args.checkpoint)
        if not res:
            print("没找到会话记录")
            return 1
        if res.get("checkpoint"):
            print("已记录读数（会话 %s）：后续导入只统计此后的增量"
                  % res["session_id"][:8])
            return 0
        if res["skipped"]:
            print("会话 %s 已是最新，无新增用量（累计未变）"
                  % res["session_id"][:8])
        else:
            print("会话 %s 新增（%s）：输入 %s · 输出 %s · 缓存读 %s"
                  % (res["session_id"][:8], args.kind,
                     usage_mod.format_int(res["input_tokens"]),
                     usage_mod.format_int(res["output_tokens"]),
                     usage_mod.format_int(res["cache_read_input_tokens"])))

    records = usage_mod.load_all(cfg)
    if not records:
        print("还没有用量记录")
        return 0
    s = usage_mod.summary(records, kind=usage_mod.KIND_CONTENT)
    dev = usage_mod.summary(records, kind=usage_mod.KIND_DEV)
    if args.json:
        print(json.dumps({"content": s, "dev": dev,
                          "records": [vars(r) for r in records]},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print("内容产出（计入）：%d 次记录 · %d 页" % (s["ops"], s["pages"]))
    print("  估算：输入 %s · 输出 %s · 合计 %s"
          % (format(s["est_in"], ","), format(s["est_out"], ","),
             format(s["est_total"], ",")))
    if s["actual_total"]:
        print("  实测：输入 %s · 输出 %s · 合计 %s（缓存读 %s）"
              % (format(s["actual_in"], ","), format(s["actual_out"], ","),
                 format(s["actual_total"], ","), format(s["actual_cache_read"], ",")))
    if dev["actual_total"] or dev["est_total"]:
        print("开发消耗（不计入）：实测 %s · 估算 %s"
              % (format(dev["actual_total"], ","), format(dev["est_total"], ",")))
    print()
    print("%-18s %-8s %-6s %-28s %10s %10s"
          % ("时间", "操作", "类别", "对象", "估算输入", "估算输出"))
    for r in records[-12:]:
        print("%-18s %-8s %-6s %-28s %10s %10s"
              % (r.ts[5:19], r.op, r.kind or "content", (r.subject or "")[:26],
                 format(r.est_in, ","), format(r.est_out, ",")))
    return 0


# ------------------------------------------------------------------ html
def cmd_html(args) -> int:
    cfg = _cfg(args)
    names = [args.collection] if args.collection else [c.name for c in cfg.collections]
    site = html_mod.Site(cfg, names)
    stats = site.build()
    print("站点 %s（%d 个 collection）"
          % (os.path.relpath(cfg.site_dir, cfg.repo_root), len(names)))
    print("页 %d · 新写 %d · 未变跳过 %d · 清理残留 %d · 图片 %d · 未建页 %d"
          % (stats.pages, stats.written, stats.skipped, stats.pruned,
             stats.images, stats.red_links))
    if args.open:
        import subprocess
        subprocess.run(["open", os.path.join(cfg.site_dir, "index.html")], check=False)
    return 0


# ------------------------------------------------------------------ lint
def cmd_lint(args) -> int:
    _rc = _run_all(args, cmd_lint)
    if _rc >= 0:
        return _rc
    cfg = _cfg(args)
    col = _collection(cfg, args)
    gl = glossary_mod.load_for(cfg)
    snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))
    pages_dir = os.path.join(cfg.obsidian.wiki_root, col.name)
    pages = verify_mod.load_pages(pages_dir)

    if not pages:
        print("还没有 wiki 页：%s" % pages_dir)
        return 0

    extra = set()
    for other in cfg.collections:
        if other.name == col.name:
            continue
        for op in verify_mod.load_pages(
                os.path.join(cfg.obsidian.wiki_root, other.name)):
            extra.add(contracts.norm_name(op.title))
            extra.add(contracts.norm_name(op.slug))
            for a in op.aliases():
                extra.add(contracts.norm_name(a))
    results = verify_mod.verify_collection(pages, snap, gl, cfg, extra)
    err, warn = verify_mod.summarize(results)

    for r in results:
        mark = "✗" if r.severity == verify_mod.ERROR else "⚠"
        print("%s [%s] %s：%s" % (mark, r.rule, r.slug, r.message))
    if results:
        print()
    print("页 %d · 错误 %d · 警告 %d" % (len(pages), err, warn))
    if args.strict and err:
        return 1
    return 0


# ------------------------------------------------------------------ status
def cmd_status(args) -> int:
    cfg = _cfg(args)
    ex = exclmod.load(cfg.obsidian.wiki_root)

    print("vault      : %s（%s）" % (cfg.obsidian.vault_root, cfg.obsidian.vault_name))
    print("wiki_root  : %s" % cfg.obsidian.wiki_root)
    print("data_dir   : %s" % cfg.data_dir)
    print("索引       : %s" % ("存在" if os.path.exists(cfg.index_db) else "未建"))
    print()

    for col in cfg.collections:
        print("collection : %s（%s）" % (col.name, col.label))
        for src in col.sources:
            issues = ex.recheck(col.name, src)
            print("  源 %s" % src)
            for i in issues:
                print("    ⚠️  [%s] %s" % (i.kind, i.message))
        if os.path.exists(cfg.index_db):
            conn = index_mod.connect(cfg.index_db)
            try:
                n_src = conn.execute(
                    "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='source'",
                    (col.name,)).fetchone()[0]
                n_page = conn.execute(
                    "SELECT COUNT(*) FROM docs WHERE collection=? AND kind='page'",
                    (col.name,)).fetchone()[0]
                print("  源文档 %d · wiki 页 %d" % (n_src, n_page))
            finally:
                conn.close()
        else:
            print("  （索引未建，运行 `llm-wiki build`）")
        print()
    return 0


# ------------------------------------------------------------------ exclude
def cmd_exclude(args) -> int:
    cfg = _cfg(args)
    root = cfg.obsidian.wiki_root
    ex = exclmod.load(root)

    if args.action == "list":
        scope = ex.global_scope
        print("全局 headings:", scope.headings)
        print("全局 globs   :", scope.globs)
        print("全局 terms   :", scope.terms)
        for col in cfg.collections:
            s = ex.scope_for(col.name)
            if not s.files:
                continue
            print("\n%s:" % col.name)
            for path, rule in s.files.items():
                print("  · %s\n      reason: %s\n      来源: %s"
                      % (path, rule.reason, rule.source_file))
        return 0

    if args.action == "why":
        col = _collection(cfg, args)
        rule = ex.match_file(col.name, args.path)
        if rule:
            print("已排除：%s" % rule.path)
            print("原因  ：%s" % rule.reason)
            if rule.canonical:
                print("对应  ：%s" % rule.canonical)
            if rule.source_file:
                print("来源  ：%s" % rule.source_file)
            return 0
        print("未被排除：%s" % args.path)
        return 1

    if args.action == "add":
        col = _collection(cfg, args)
        rule = exclmod.add_file_rule(root, col.name, args.path, args.reason,
                                     canonical=args.canonical or "",
                                     source_root=col.sources[0])
        print("已加入排除名单：%s\n  reason: %s" % (rule.path, rule.reason))
        return 0

    if args.action == "remove":
        col = _collection(cfg, args)
        ok = exclmod.remove_file_rule(root, col.name, args.path)
        print("已移除" if ok else "名单里没有这条", args.path)
        return 0

    return 2


# ------------------------------------------------------------------ extract
def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _review_dir(cfg, collection: str) -> str:
    return os.path.join(cfg.data_dir, "review", collection)


def cmd_extract(args) -> int:
    """S1+S2：候选生成与确定性门禁，产出 proposer 工单。

    `--global` 时不按 collection 切开：扫全部源文档，词表/注册表共用，
    每条证据记录它来自哪个 collection。
    """
    global_mode = bool(getattr(args, "global_", False))
    if not global_mode:
        _rc = _run_all(args, cmd_extract)
        if _rc >= 0:
            return _rc
    cfg = _cfg(args)
    ex = exclmod.load(cfg.obsidian.wiki_root)
    lex = glossary_mod.lexicon_for(cfg)
    reg = registry_mod.load(os.path.join(cfg.obsidian.wiki_root, "entities.yaml"))

    if global_mode:
        if getattr(args, "changed_only", False):
            print("--global 暂不支持 --changed-only；请分 collection 增量。",
                  file=sys.stderr)
            return 2
        col_name = "_global"
        docs, pages = [], []
        for c in cfg.collections:
            docs.extend(norm_mod.scan_collection(c, cfg, ex).docs)
            pages.extend(verify_mod.load_pages(
                os.path.join(cfg.obsidian.wiki_root, c.name)))
        doc_ids = None
    else:
        col = _collection(cfg, args)
        col_name = col.name
        report = norm_mod.scan_collection(col, cfg, ex)
        docs = report.docs
        pages = verify_mod.load_pages(os.path.join(cfg.obsidian.wiki_root, col.name))
        doc_ids = None
        if getattr(args, "changed_only", False):
            conn = state_mod.connect(cfg.state_db)
            try:
                prev = state_mod.load_sources(conn, col.name)
            finally:
                conn.close()
            doc_ids = {
                d.doc_id for d in docs
                if d.doc_id not in prev or prev[d.doc_id].sha256 != d.sha256
            }
            if not doc_ids:
                print("没有新增或变更的源文档，跳过抽取。")
                return 0

    cands = extract_mod.generate_candidates(docs, lex, doc_ids)
    extract_mod.apply_gates(cands, extract_mod.index_pages(pages), reg, lex)

    out_dir = os.path.join(cfg.orders_dir, col_name, "extract")
    _write_json(os.path.join(out_dir, "_candidates.json"),
                {"collection": col_name,
                 "candidates": [c.as_dict() for c in
                                sorted(cands.values(), key=lambda x: -x.score)]})

    propose = extract_mod.build_propose_order(
        col_name, list(cands.values()), int(getattr(args, "max_candidates", 200)))
    _write_json(os.path.join(out_dir, "_propose.json"), propose)

    summary = extract_mod.candidate_summary(cands.values())
    rdir = _review_dir(cfg, col_name)
    os.makedirs(rdir, exist_ok=True)
    _write_json(os.path.join(rdir, "_pending.json"),
                {"collection": col_name, "summary": summary,
                 "pending": review_mod.pending_report(cands.values())})

    print("collection : %s" % col_name)
    print("候选 %d · 已覆盖 %d · 观察池 %d · 待复核 %d"
          % (summary["total"], summary["covered"],
             summary["observation"], summary["needs_review"]))
    if doc_ids is not None:
        print("增量范围：%d 篇源文档" % len(doc_ids))
    print("候选明细   → %s" % os.path.relpath(
        os.path.join(out_dir, "_candidates.json"), cfg.repo_root))
    print("proposer 工单 → %s（%d 条）" % (
        os.path.relpath(os.path.join(out_dir, "_propose.json"), cfg.repo_root),
        len(propose["candidates"])))
    print("预复核清单 → %s" % os.path.relpath(
        os.path.join(rdir, "_pending.json"), cfg.repo_root))
    if summary["reasons"]:
        print("\n门禁原因：")
        for reason, n in list(summary["reasons"].items())[:12]:
            print("  %-28s %d" % (reason, n))
    print("\n下一步：Claude 按 _propose.json 写 proposals.json，"
          "再跑 `review build` 生成盲审工单。")
    return 0


# ------------------------------------------------------------------ review
def _human_apply(args) -> int:
    """把人审 HTML 导出的 decisions JSON 回灌到各 collection。"""
    cfg = _cfg(args)
    path = args.file
    if not os.path.exists(path):
        print("文件不存在：%s" % path, file=sys.stderr)
        return 2
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print("不是有效 JSON：%s" % exc, file=sys.stderr)
        return 2
    rows = raw if isinstance(raw, list) else (raw.get("decisions") or [])

    groups: dict = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("candidate_id"):
            continue
        if r.get("reviewed") is False:
            continue
        groups.setdefault(str(r.get("collection") or ""), []).append(r)

    rc = 0
    for collection, decisions in groups.items():
        if not collection:
            print("跳过缺少 collection 的决策 %d 条" % len(decisions))
            continue
        cfile = os.path.join(cfg.orders_dir, collection, "extract", "_candidates.json")
        if not os.path.exists(cfile):
            print("找不到候选文件：%s" % cfile, file=sys.stderr)
            rc = 2
            continue
        candidates = review_mod.load_candidates(cfile)
        propose_path = os.path.join(cfg.orders_dir, collection, "extract", "_propose.json")
        if os.path.exists(propose_path):
            packet = json.load(open(propose_path, encoding="utf-8"))
            ids = {str(c.get("candidate_id")) for c in packet.get("candidates") or []}
            candidates = {k: v for k, v in candidates.items() if k in ids}

        resolutions = []
        handled = set()
        for d in decisions:
            cid = str(d["candidate_id"])
            c = candidates.get(cid)
            if not c:
                print("跳过未知候选：%s" % cid)
                continue
            handled.add(cid)
            resolutions.append(review_mod.Resolution(
                candidate_id=cid,
                surface=str(c.get("surface") or d.get("canonical") or cid),
                action=str(d.get("action") or "defer"),
                canonical=str(d.get("canonical") or c.get("surface") or ""),
                type=str(d.get("type") or "entity"),
                aliases=[str(x) for x in (d.get("aliases") or [])],
                merge_into=str(d.get("merge_into") or ""),
                qualifier=str(d.get("qualifier") or ""),
                confidence=float(d.get("confidence") or 1.0),
                reviewer="human",
                evidence=c.get("evidence") or [],
            ))

        # 没在本次导入里出现的人审项继续留在队列里
        qpath = os.path.join(cfg.data_dir, "review", collection, "_human_queue.json")
        if os.path.exists(qpath):
            try:
                q = json.load(open(qpath, encoding="utf-8"))
            except json.JSONDecodeError:
                q = {}
            for reason, items in (q.get("groups") or {}).items():
                for it in items:
                    cid = str(it.get("candidate_id") or "")
                    if not cid or cid in handled:
                        continue
                    resolutions.append(review_mod.Resolution(
                        candidate_id=cid,
                        surface=str(it.get("surface") or cid),
                        action=str(it.get("action") or "defer"),
                        canonical=str(it.get("canonical") or ""),
                        type=str(it.get("type") or "entity"),
                        aliases=[str(x) for x in (it.get("aliases") or [])],
                        merge_into=str(it.get("merge_into") or ""),
                        qualifier=str(it.get("qualifier") or ""),
                        confidence=float(it.get("confidence") or 0.0),
                        reviewer="human",
                        objections=it.get("objections") or [],
                        evidence=it.get("evidence") or [],
                        human_reason=reason,
                        human_options=it.get("human_options") or [],
                    ))

        stats = review_mod.apply_resolutions(cfg, collection, resolutions, candidates)
        print("%s：裁决 %d · 人审 %d · 建页 %d · 注册表 %d"
              % (collection, stats["resolutions"], stats["human"],
                 stats["orders"], stats["registry_changes"]))
        if stats["human"]:
            print("  人审队列 → %s" % os.path.relpath(
                stats["human_queue"]["path"], cfg.repo_root))
        prune = review_mod.prune_orders(cfg)
        print("  清理过期 order %d · 降级 active %d"
              % (len(prune["removed"]), len(prune["deferred"])))
    return rc


def cmd_review(args) -> int:
    if args.action == "html":
        cfg = _cfg(args)
        out = human_html.build(
            cfg, out_path=getattr(args, "out", None),
            collection=getattr(args, "collection", None),
            standalone=not getattr(args, "fragment", False))
        print("人审工作台 → %s" % os.path.relpath(out, cfg.repo_root))
        print("直接用浏览器打开这个文件；勾选/备注后导出 JSON，再跑：")
        print("  python -m wikilib.cli review human-apply --file <导出的 JSON>")
        if getattr(args, "open", False):
            webbrowser.open("file://" + os.path.abspath(out))
        return 0
    if args.action == "human-apply":
        return _human_apply(args)
    if args.action == "evidence":
        cfg = _cfg(args)
        stats = review_mod.enrich_orders(
            cfg, collection=getattr(args, "collection", None),
            force=getattr(args, "force", False))
        print("orders %d · 已回填 %d · 已有跳过 %d · 无证据 %d"
              % (stats["orders"], stats["enriched"],
                 stats["skipped"], stats["missing"]))
        return 0
    if args.action == "prune":
        cfg = _cfg(args)
        stats = review_mod.prune_orders(cfg, dry_run=getattr(args, "dry_run", False))
        for r in stats["removed"]:
            print("  移出 %s（%s）" % (r["slug"], r["reason"]))
        print("过期 order %d · 降级 active %d"
              % (len(stats["removed"]), len(stats["deferred"])))
        return 0

    global_mode = bool(getattr(args, "global_", False))
    if not global_mode:
        _rc = _run_all(args, cmd_review)
        if _rc >= 0:
            return _rc
    cfg = _cfg(args)
    col_name = "_global" if global_mode else _collection(cfg, args).name
    rdir = _review_dir(cfg, col_name)
    cfile = os.path.join(cfg.orders_dir, col_name, "extract", "_candidates.json")
    candidates = review_mod.load_candidates(cfile)
    # `_candidates.json` 是全量候选；本次只复核 `_propose.json` 里的那批。
    # 不过滤的话，未送审的候选会被误判成 missing_proposal，淹没真正的人审项。
    propose_path = os.path.join(cfg.orders_dir, col_name, "extract", "_propose.json")
    if os.path.exists(propose_path):
        packet = json.load(open(propose_path, encoding="utf-8"))
        packet_ids = {str(c.get("candidate_id")) for c in packet.get("candidates") or []}
        if packet_ids:
            candidates = {k: v for k, v in candidates.items() if k in packet_ids}

    if args.action == "build":
        proposals = review_mod._load_json_list(
            args.proposals or os.path.join(rdir, "proposals.json"))
        if not proposals:
            print("没有 proposals.json。先让 Claude 按 _propose.json 产出提案。",
                  file=sys.stderr)
            return 2
        # 默认只盲审「会改变 wiki 内容」的决定；reject/defer 不产生页面，
        # 不值得再花一轮模型调用。
        if not getattr(args, "all", False):
            proposals = [p for p in proposals
                         if str(p.get("action") or "") not in ("reject", "defer")]
        parsed = {}
        for c in candidates.values():
            parsed[c.get("candidate_id")] = c
        # 用 extract.Candidate 对象补证据：从 _candidates.json 还原
        from .extract import Candidate, Evidence
        objs = {}
        for cid, c in candidates.items():
            obj = Candidate(key=c.get("key", cid), surface=c.get("surface", cid),
                            gate=c.get("gate", "needs_review"),
                            gate_reason=c.get("gate_reason", ""),
                            existing_page=c.get("existing_page", ""))
            obj.candidate_id = cid
            for e in c.get("evidence") or []:
                obj.evidence.append(Evidence(
                    doc_id=e.get("doc_id", ""), doc_title=e.get("doc", ""),
                    heading=e.get("heading", ""), heading_path=e.get("heading_path", ""),
                    quote=e.get("quote", ""), offset=int(e.get("offset") or 0),
                    definitional=bool(e.get("definitional")),
                    in_code=bool(e.get("in_code")), in_table=bool(e.get("in_table"))))
            objs[cid] = obj
        order = extract_mod.build_challenge_order(col_name, proposals, objs)
        path = os.path.join(rdir, "_challenge.json")
        _write_json(path, order)
        print("盲审工单 → %s（%d 条）" % (os.path.relpath(path, cfg.repo_root),
                                          len(order["items"])))
        print("把这份文件交给另一个独立上下文（Challenger），不要让它读 proposals.json。")
        return 0

    if args.action == "apply":
        proposals = review_mod._load_json_list(
            args.proposals or os.path.join(rdir, "proposals.json"))
        challenges = review_mod._load_json_list(
            args.challenges or os.path.join(rdir, "challenges.json"))
        arbiters = review_mod._load_json_list(
            args.arbiters or os.path.join(rdir, "arbiters.json"))
        if not proposals and not challenges:
            print("没有 proposals/challenges，无法合并。", file=sys.stderr)
            return 2
        raw_dir = os.path.join(cfg.raw_dir, col_name)
        if global_mode:
            collection_docs = 99
        else:
            collection_docs = (len([f for f in os.listdir(raw_dir) if f.endswith(".md")])
                               if os.path.isdir(raw_dir) else 0)
        resolutions = review_mod.resolve(proposals, challenges, arbiters, candidates,
                                         collection_docs=collection_docs)
        stats = review_mod.apply_resolutions(cfg, col_name, resolutions, candidates,
                                             global_mode=global_mode)
        print("裁决 %d · 人审 %d · 建页工单 %d · 注册表变更 %d"
              % (stats["resolutions"], stats["human"], stats["orders"],
                 stats["registry_changes"]))
        print("注册表   → %s" % os.path.relpath(stats["registry_path"], cfg.repo_root))
        if stats["human"]:
            print("人审队列 → %s" % os.path.relpath(stats["human_queue"]["path"],
                                                    cfg.repo_root))
            for reason, n in stats["human_queue"]["groups"].items():
                print("  %-28s %d" % (reason, n))
        if stats["order_paths"]:
            print("\n建页工单：")
            for p in stats["order_paths"]:
                print("  · %s" % os.path.relpath(p, cfg.repo_root))
        print("\n下一步：按 order 写页，再跑 `build` 与 `lint --strict`。")
        return 0

    if args.action == "pending":
        data = json.load(open(os.path.join(rdir, "_pending.json"), encoding="utf-8")) \
            if os.path.exists(os.path.join(rdir, "_pending.json")) else {}
        rows = data.get("pending") or []
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print("待复核 %d 条（按来源覆盖排序）\n" % len(rows))
        for r in rows:
            print("%-28s %-34s %s"
                  % (r["surface"][:28], r["reason"][:34],
                     "doc=%d mention=%d" % (r["doc_coverage"], r["mentions"])))
        return 0

    if args.action == "human":
        path = os.path.join(rdir, "_human_queue.md")
        if not os.path.exists(path):
            print("还没有人审队列。先跑 `review apply`。", file=sys.stderr)
            return 2
        print(open(path, encoding="utf-8").read())
        return 0

    if args.action == "status":
        files = {
            "候选": os.path.join(cfg.orders_dir, col_name, "extract", "_candidates.json"),
            "proposer 提案": os.path.join(rdir, "proposals.json"),
            "盲审工单": os.path.join(rdir, "_challenge.json"),
            "challenger 结果": os.path.join(rdir, "challenges.json"),
            "最终裁决": os.path.join(rdir, "_resolved.json"),
            "人审队列": os.path.join(rdir, "_human_queue.md"),
        }
        print("collection : %s" % col_name)
        for label, path in files.items():
            print("  %-16s %s" % (label, "✓ " + os.path.relpath(path, cfg.repo_root)
                                  if os.path.exists(path) else "—"))
        return 0

    return 2


# ------------------------------------------------------------------ registry
def cmd_registry(args) -> int:
    cfg = _cfg(args)
    path = os.path.join(cfg.obsidian.wiki_root, "entities.yaml")
    reg = registry_mod.load(path)

    if args.action == "list":
        if args.json:
            print(json.dumps([e.as_dict() for e in reg.active()],
                             ensure_ascii=False, indent=2))
            return 0
        print("实体注册表：%s（%d 条 active）" % (
            os.path.relpath(path, cfg.repo_root), len(reg.active())))
        for e in reg.active():
            alias = ", ".join(e.aliases[:3])
            print("  %-12s %-32s %s" % (e.type, e.canonical, alias))
        return 0

    if args.action == "show":
        e = reg.find(args.name)
        if not e:
            print("注册表里没有：%s" % args.name, file=sys.stderr)
            return 2
        print(json.dumps(e.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.action == "check":
        conflicts = reg.conflicts()
        if not conflicts:
            print("注册表无同名冲突（%d 条）。" % len(reg.entries))
            return 0
        print("发现 %d 处同名冲突：" % len(conflicts))
        for c in conflicts:
            print("  「%s」 → %s（类型：%s）"
                  % (c["surface"], ", ".join(c["entries"]), "/".join(c["types"])))
        return 1

    return 2


# ------------------------------------------------------------------ lexicon
def cmd_lexicon(args) -> int:
    cfg = _cfg(args)
    path = os.path.join(cfg.obsidian.wiki_root, "GLOSSARY.md")
    lex = lexicon_mod.load(path)

    if args.action == "list":
        if args.json:
            print(json.dumps([e.as_dict() for e in lex.all_entries()],
                             ensure_ascii=False, indent=2))
            return 0
        print("词汇表：%s（%d 条）" % (os.path.relpath(path, cfg.repo_root),
                                      len(lex.entries)))
        for kind in ("concept", "proper", "acronym"):
            items = [e for e in lex.all_entries() if e.kind == kind]
            if not items:
                continue
            print("\n## %s（%d）" % (lexicon_mod.KIND_LABEL.get(kind, kind), len(items)))
            for e in items:
                extra = e.expansion or ", ".join(e.aliases[:3])
                print("  %-32s %-24s %s" % (e.canonical, e.zh or "—", extra))
        return 0

    if args.action == "add":
        kind = {"concept": "concept", "proper": "proper",
                "acronym": "acronym", "common": "common"}[args.kind]
        e = lexicon_mod.add_entry(
            path, canonical=args.name, kind=kind, zh=args.zh or "",
            aliases=args.alias or [], expansion=args.expansion or "",
            note=args.note or "", banned=args.banned or [])
        print("已写入词汇表：%s（%s）" % (e.canonical, e.kind))
        return 0

    if args.action == "provenance":
        res = lexicon_mod.provenance(path, args.term, cfg.raw_dir)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        total = sum(len(v) for v in res.values())
        print("「%s」出处：%d 个 collection / %d 处" % (args.term, len(res), total))
        for collection, items in res.items():
            print("\n## %s（%d）" % (collection, len(items)))
            for it in items[:20]:
                print("  - %s # %s" % (it["doc"][:50], it["heading"][:50]))
                print("    %s" % it["quote"][:180])
        if not res:
            print("没有找到出处；这个词只存在于词表，没有任何源文档支撑。")
        return 0

    if args.action == "missing":
        # 扫描快照，找未登记的缩写
        root = cfg.raw_dir
        found = {}
        if os.path.isdir(root):
            for dirpath, _, files in os.walk(root):
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    text = open(os.path.join(dirpath, name), encoding="utf-8").read()
                    for tok in lex.check_acronyms(text):
                        found[tok] = found.get(tok, 0) + 1
        rows = sorted(found.items(), key=lambda x: -x[1])
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print("未登记缩写 %d 个：" % len(rows))
        for tok, n in rows[:60]:
            print("  %-16s %d" % (tok, n))
        return 0

    if args.action == "check":
        # 1) 同一写法是否被多个 canonical 占用
        seen = {}
        for e in lex.all_entries():
            for f in e.all_forms():
                seen.setdefault(lexicon_mod.normalize_term(f), set()).add(e.canonical)
        conflicts = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
        # 2) 缩写是否有全称
        missing_exp = [e.canonical for e in lex.all_entries()
                       if e.kind == "acronym" and not e.expansion]
        print("条目 %d · 写法冲突 %d · 缩写缺全称 %d"
              % (len(lex.entries), len(conflicts), len(missing_exp)))
        for k, v in list(conflicts.items())[:20]:
            print("  冲突：%s → %s" % (k, ", ".join(v)))
        for name in missing_exp[:20]:
            print("  缺全称：%s" % name)
        return 1 if (conflicts or missing_exp) else 0

    return 2


# ------------------------------------------------------------------ main
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="llm-wiki", description="LLM Wiki 编译管道")
    ap.add_argument("--config", default=None, help="改用指定的 config.local.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("normalize", "build", "status", "lint", "html", "update",
                 "stamp", "usage"):
        p = sub.add_parser(name)
        p.add_argument("--collection", default=None)
        if name == "normalize":
            p.add_argument("--dry-run", action="store_true")
        if name == "lint":
            p.add_argument("--strict", action="store_true")
        if name == "html":
            p.add_argument("--open", action="store_true")
        if name in ("update", "stamp"):
            p.add_argument("--force", action="store_true")
        if name == "update":
            p.add_argument("--inventory", action="store_true",
                           help="强制重出页面清单任务")
            p.add_argument("--no-evidence", action="store_true",
                           help="不生成 evidence packet（默认生成）")
        if name == "usage":
            p.add_argument("--json", action="store_true")
            p.add_argument("--import-dir", default=None,
                           help="导入 Claude Code 会话记录目录")
            p.add_argument("--session", default=None, help="指定会话 id")
            p.add_argument("--kind", default="content",
                           choices=["content", "dev"],
                           help="content=产出 wiki 内容；dev=开发 wiki 本身")
            p.add_argument("--import", dest="import_now", action="store_true",
                           help="导入当前会话的增量（自动定位最近的会话记录）")
            p.add_argument("--checkpoint", action="store_true",
                           help="只记录当前读数，不统计（用于划出内容产出的起点）")
        p.set_defaults(func={"normalize": cmd_normalize, "build": cmd_build,
                             "status": cmd_status, "lint": cmd_lint,
                             "html": cmd_html, "update": cmd_update,
                             "stamp": cmd_stamp, "usage": cmd_usage}[name])

    p = sub.add_parser("query")
    p.add_argument("text")
    p.add_argument("--json", action="store_true")
    p.add_argument("--collection", default=None)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--strict", action="store_true", help="出错时抛异常，不返回空列表")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("init")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("note")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", default=None)
    p.add_argument("--collection", default=None)
    p.add_argument("--origin", default="conversation",
                   choices=list(contracts.NOTE_ORIGINS))
    p.add_argument("--status", default="draft",
                   choices=["active", "draft", "disputed"])
    p.add_argument("--body", default=None, help="从 Markdown 文件导入正文")
    p.add_argument("--link", default=None, help="关联页标题，逗号分隔")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("collection")
    ca = p.add_subparsers(dest="action", required=True)
    lp = ca.add_parser("list")
    lp.add_argument("--json", action="store_true")
    for action in ("add", "remove"):
        cp = ca.add_parser(action)
        cp.add_argument("name", nargs="?")
        if action == "add":
            cp.add_argument("--source", required=True)
            cp.add_argument("--label", default=None)

    p.set_defaults(func=cmd_collection)

    p = sub.add_parser("exclude")
    e = p.add_subparsers(dest="action", required=True)
    for action in ("list", "why", "add", "remove"):
        ep = e.add_parser(action)
        ep.add_argument("--collection", default=None)
        if action != "list":
            ep.add_argument("path")
        if action == "add":
            ep.add_argument("--reason", required=True)
            ep.add_argument("--canonical", default=None)
    p.set_defaults(func=cmd_exclude)

    p = sub.add_parser("extract", help="候选抽取 + 确定性门禁，产出 proposer 工单")
    p.add_argument("--collection", default=None)
    p.add_argument("--max-candidates", type=int, default=200,
                   help="本次送复核的候选上限")
    p.add_argument("--changed-only", action="store_true",
                   help="只处理新增/变更的源文档")
    p.add_argument("--global", dest="global_", action="store_true",
                   help="跨 collection 统一抽取，词表/注册表共用")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("review", help="对抗复核：盲审工单、合并裁决、人审队列")
    ra = p.add_subparsers(dest="action", required=True)
    for action in ("build", "apply", "pending", "human", "status",
                   "html", "human-apply", "prune", "evidence"):
        rp = ra.add_parser(action)
        rp.add_argument("--collection", default=None)
        rp.add_argument("--global", dest="global_", action="store_true",
                        help="使用 _global 候选池")
        if action in ("build", "apply"):
            rp.add_argument("--proposals", default=None)
            rp.add_argument("--challenges", default=None)
            rp.add_argument("--arbiters", default=None)
        if action == "build":
            rp.add_argument("--all", action="store_true",
                            help="连 reject/defer 也生成盲审工单（默认跳过）")
        if action == "pending":
            rp.add_argument("--json", action="store_true")
        if action == "html":
            rp.add_argument("--out", default=None,
                            help="输出 HTML 路径（默认 data/review/human_review.html）")
            rp.add_argument("--open", action="store_true", help="生成后用浏览器打开")
            rp.add_argument("--fragment", action="store_true",
                            help="输出片段（供 Artifact 发布），默认完整 HTML")
        if action == "human-apply":
            rp.add_argument("--file", required=True,
                            help="人审工作台导出的 decisions JSON")
        if action == "prune":
            rp.add_argument("--dry-run", action="store_true")
        if action == "evidence":
            rp.add_argument("--force", action="store_true",
                            help="覆盖已有 evidence packet")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("registry", help="实体注册表 entities.yaml")
    rg = p.add_subparsers(dest="action", required=True)
    for action in ("list", "check"):
        rp = rg.add_parser(action)
        rp.add_argument("--json", action="store_true")
    rp = rg.add_parser("show")
    rp.add_argument("name")
    p.set_defaults(func=cmd_registry)

    p = sub.add_parser("lexicon", help="词汇表：概念/专有名词/缩写")
    lx = p.add_subparsers(dest="action", required=True)
    for action in ("list", "check", "missing"):
        lp = lx.add_parser(action)
        lp.add_argument("--json", action="store_true")
    lp = lx.add_parser("provenance")
    lp.add_argument("term")
    lp.add_argument("--json", action="store_true")
    lp = lx.add_parser("add")
    lp.add_argument("--name", required=True)
    lp.add_argument("--kind", required=True,
                    choices=["concept", "proper", "acronym", "common"])
    lp.add_argument("--zh", default=None)
    lp.add_argument("--alias", action="append", default=[])
    lp.add_argument("--expansion", default=None)
    lp.add_argument("--note", default=None)
    lp.add_argument("--banned", action="append", default=[])
    p.set_defaults(func=cmd_lexicon)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except cfgmod.ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return cfgmod.EXIT_CONFIG
    except exclmod.ExclusionError as exc:
        print("排除名单错误：%s" % exc, file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
