"""命令行入口：`python -m wikilib.cli <command>`。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from . import chunk as chunk_mod
from . import config as cfgmod
from . import exclusions as exclmod
from . import glossary as glossary_mod
from . import index as index_mod
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


# ------------------------------------------------------------------ normalize
def cmd_normalize(args) -> int:
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
        state_mod.save_sources(state_conn, col.name, _states(report))
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
                headings=[{"norm": h.norm, "orig": h.orig} for h in d.headings])
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
        for slug, imps in by_page.items():
            if slug == "(候选)":
                continue
            path = next((p.path for p in pages if p.slug == slug), None)
            existing = open(path, encoding="utf-8").read() if path else ""
            srcs = []
            for imp in imps:
                st = current.get(imp.doc_id)
                if st:
                    srcs.append({"doc_id": st.doc_id, "rel_path": st.rel_path,
                                 "headings": st.headings})
            orders.append(plan_mod.build_order(slug, imps, existing, srcs, excerpt))

        paths = plan_mod.write_orders(orders, os.path.join(cfg.orders_dir, col.name))
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
            with open(path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post) + "\n")

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
    if args.import_dir or args.session:
        project_dir = os.path.expanduser(args.import_dir) if args.import_dir \
            else _latest_project_dir()
        if not project_dir:
            print("没找到任何会话记录")
            return 1
        res = usage_mod.import_session(cfg, project_dir, args.session)
        if not res:
            print("没找到会话记录")
            return 1
        if res["skipped"]:
            print("会话 %s 已是最新，无新增用量（累计未变）"
                  % res["session_id"][:8])
        else:
            print("会话 %s 新增：输入 %s · 输出 %s · 缓存读 %s"
                  % (res["session_id"][:8],
                     usage_mod.format_int(res["input_tokens"]),
                     usage_mod.format_int(res["output_tokens"]),
                     usage_mod.format_int(res["cache_read_input_tokens"])))

    records = usage_mod.load_all(cfg)
    if not records:
        print("还没有用量记录")
        return 0
    s = usage_mod.summary(records)
    if args.json:
        print(json.dumps({"summary": s, "records": [vars(r) for r in records]},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print("累计 %d 次记录 · %d 页产出" % (s["ops"], s["pages"]))
    print("  估算：输入 %s · 输出 %s · 合计 %s"
          % (format(s["est_in"], ","), format(s["est_out"], ","),
             format(s["est_total"], ",")))
    if s["actual_total"]:
        print("  实测：输入 %s · 输出 %s · 合计 %s（缓存读 %s）"
              % (format(s["actual_in"], ","), format(s["actual_out"], ","),
                 format(s["actual_total"], ","), format(s["actual_cache_read"], ",")))
    print()
    print("%-20s %-8s %-34s %10s %10s" % ("时间", "操作", "对象", "估算输入", "估算输出"))
    for r in records[-12:]:
        print("%-20s %-8s %-34s %10s %10s"
              % (r.ts[5:19], r.op, (r.subject or "")[:32],
                 format(r.est_in, ","), format(r.est_out, ",")))
    return 0


# ------------------------------------------------------------------ html
def cmd_html(args) -> int:
    cfg = _cfg(args)
    col = _collection(cfg, args)
    snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))
    site = html_mod.Site(cfg, col.name, snap)
    stats = site.build()
    print("站点 %s" % os.path.relpath(cfg.site_dir, cfg.repo_root))
    print("页 %d · 新写 %d · 未变跳过 %d · 清理残留 %d · 图片 %d · 未建页 %d"
          % (stats.pages, stats.written, stats.skipped, stats.pruned,
             stats.images, stats.red_links))
    if args.open:
        import subprocess
        subprocess.run(["open", os.path.join(cfg.site_dir, "index.html")], check=False)
    return 0


# ------------------------------------------------------------------ lint
def cmd_lint(args) -> int:
    cfg = _cfg(args)
    col = _collection(cfg, args)
    gl = glossary_mod.load_for(cfg)
    snap = verify_mod.load_snapshot(os.path.join(cfg.raw_dir, col.name))
    pages_dir = os.path.join(cfg.obsidian.wiki_root, col.name)
    pages = verify_mod.load_pages(pages_dir)

    if not pages:
        print("还没有 wiki 页：%s" % pages_dir)
        return 0

    results = verify_mod.verify_collection(pages, snap, gl, cfg)
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
        if name == "usage":
            p.add_argument("--json", action="store_true")
            p.add_argument("--import-dir", default=None,
                           help="导入 Claude Code 会话记录目录")
            p.add_argument("--session", default=None, help="指定会话 id")
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
