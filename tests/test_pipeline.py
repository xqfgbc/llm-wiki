"""回归测试：把这次踩过的坑固化成断言。

跑法：
    "$WIKI_PYTHON" -m unittest discover -s tests -v

全部使用临时 vault，不触碰真实数据。
"""
import json
import os
import shutil
import tempfile
import unittest

import yaml

from wikilib import (chunk, config, contracts, exclusions, index, normalize,
                     pages, plan, query, state, usage, verify)


CLIP = """---
title: "剪藏文章"
source: "https://example.com/a"
tags:
  - "clippings"
---
正文开头。

## New Attack SurfaceThe STS keeps all of this in a database. Default is PostgreSQL.

一些内容。

## Normal Heading

另一些内容。

## **带格式的标题**

最后一段。
"""

PLAIN = """---
title: "PPT 派生文档"
source: "deck.pptx"
document-type: ETB
---
## Slide 1: Overview

正文。

## Slide 22: AI Leadership & Op. Model 挑战 1：C-suite

正文。

## A. 决策小队网络（DECISION SQUADS）

正文。
"""

NOHEAD = """---
title: "公众号剪藏"
source: "https://mp.weixin.qq.com/s/x"
tags:
  - "clippings"
---
**导读** 一段导语。

**主要内容包括以下几个部分：**

**本体建设的成本，为什么一直卡在数据打通之后**

正文一。

**从 Schema 到业务实体**

正文二。
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, ".obsidian"))
        self.wiki = os.path.join(self.vault, "wiki")
        os.makedirs(self.wiki)
        self.src = os.path.join(self.vault, "notes")
        os.makedirs(self.src)
        self.src2 = os.path.join(self.vault, "docs2")
        os.makedirs(self.src2)
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "version": 1,
                "obsidian": {"vault_root": self.vault, "vault_name": "vault",
                             "wiki_root": self.wiki},
                "data_dir": os.path.join(self.tmp, "data"),
                "collections": [
                    {"name": "notes", "label": "笔记", "sources": [self.src]},
                ],
            }, f, allow_unicode=True)
        self.cfg = config.load(self.cfg_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # 便捷方法
    def write(self, name, text, where=None):
        path = os.path.join(where or self.src, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class TestNormalize(Base):
    def test_doc_id_is_filename_not_dir(self):
        """一个目录多个 md 时 doc_id 不能塌成目录名。"""
        self.write("a.md", CLIP)
        self.write("b.md", PLAIN)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        ids = {d.doc_id for d in rep.docs}
        self.assertEqual(ids, {"a", "b"})

    def test_repair_only_for_clippings(self):
        """粘连修复只对剪藏生效，PPT 文档的标题一个字都不许动。"""
        self.write("clip.md", CLIP)
        self.write("deck.md", PLAIN)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        by_id = {d.doc_id: d for d in rep.docs}

        repaired = [h for h in by_id["clip"].headings if h.repaired]
        self.assertTrue(repaired, "剪藏应修复粘连标题")
        self.assertEqual(repaired[0].norm, "New Attack Surface")

        self.assertFalse([h for h in by_id["deck"].headings if h.repaired],
                         "PPT 文档不应被修复")
        texts = [h.norm for h in by_id["deck"].headings]
        self.assertIn("Slide 22: AI Leadership & Op. Model 挑战 1：C-suite", texts)
        self.assertIn("A. 决策小队网络（DECISION SQUADS）", texts)

    def test_heading_markup_stripped(self):
        self.write("clip.md", CLIP)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        norms = [h.norm for d in rep.docs for h in d.headings]
        self.assertIn("带格式的标题", norms)

    def test_promote_bold_only_when_no_headings(self):
        """无标题的剪藏才把加粗行提升为小节，引导句不提升。"""
        self.write("nohead.md", NOHEAD)
        self.write("clip.md", CLIP)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        by_id = {d.doc_id: d for d in rep.docs}

        norms = [h.norm for h in by_id["nohead"].headings]
        self.assertIn("本体建设的成本，为什么一直卡在数据打通之后", norms)
        self.assertNotIn("主要内容包括以下几个部分：", norms)
        self.assertTrue(all(h.synthetic for h in by_id["nohead"].headings))

        # 只有「文档标题」这一条隐式标题允许是 synthetic；
        # 有标题的文档不应把加粗行也提升成小节。
        extra = [h for h in by_id["clip"].headings
                 if h.synthetic and h.norm != by_id["clip"].title]
        self.assertFalse(extra, "已有标题的文档不应触发加粗行提升")

    def test_exclusions_apply(self):
        self.write("skip.md", PLAIN)
        self.write("keep.md", CLIP)
        exclusions.add_file_rule(self.wiki, "notes", "skip.md", "测试排除", source_root=self.src)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        self.assertEqual([d.doc_id for d in rep.docs], ["keep"])

    def test_exclusion_requires_reason(self):
        with open(os.path.join(self.wiki, "EXCLUSIONS.yaml"), "w", encoding="utf-8") as f:
            f.write("version: 1\ncollections:\n  notes:\n    files:\n      - path: x.md\n")
        with self.assertRaises(exclusions.ExclusionError):
            exclusions.load(self.wiki)


class TestTitleFallback(Base):
    def test_many_h1_falls_back_to_filename(self):
        self.write("multi-h1.md",
                   "## A\n\nx\n\n# 第一部分\n\ny\n\n# 第二部分\n\nz\n")
        self.write("single-h1.md", "## A\n\nx\n\n# 唯一标题\n\ny\n")
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        by_id = {d.doc_id: d for d in rep.docs}
        self.assertEqual(by_id["multi-h1"].title, "multi-h1")
        self.assertEqual(by_id["single-h1"].title, "唯一标题")

    def test_h1_inside_fence_ignored(self):
        self.write("fenced.md",
                   "## A\n\n```bash\n# 这是 shell 注释\nls\n```\n\n正文\n")
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        self.assertEqual(rep.docs[0].title, "fenced",
                         "代码块里的 # 不是标题")


class TestChineseSectionHeadings(Base):
    def test_cn_numbered_lines_promoted(self):
        self.write("cn.md", """---
title: 中文长文
source: https://example.com/cn
tags: [clippings]
---
一、无状态假设的三条前提

正文一。

二、Agent 的状态清单

正文二。
""")
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        norms = [h.norm for h in rep.docs[0].headings]
        self.assertIn("一、无状态假设的三条前提", norms)
        self.assertIn("二、Agent 的状态清单", norms)


class TestTitleAsHeading(Base):
    def test_headless_doc_title_is_citable(self):
        """没有小节的文档，文档标题要能作为引用锚点解析。"""
        self.write("noh.md", NOHEAD)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        heads = {h.norm for h in rep.docs[0].headings}
        self.assertIn(rep.docs[0].title, heads)


class TestChunk(Base):
    def test_no_chunk_crosses_fence(self):
        body = "## A\n\n```mermaid\ngraph TD\nA-->B\n```\n\n" + "文字" * 2000
        text = "---\ntitle: T\nsource: https://x\ntags: [clippings]\n---\n" + body
        self.write("f.md", text)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        chunks = chunk.chunk_document(rep.docs[0], self.cfg)
        for c in chunks:
            self.assertEqual(c.body.count("```") % 2, 0,
                             "切片把代码块切开了：%s" % c.chunk_id)

    def test_hard_limit_respected(self):
        self.write("long.md", NOHEAD)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        chunks = chunk.chunk_document(rep.docs[0], self.cfg)
        hard = int(self.cfg.get("hard_chunk_chars"))
        self.assertTrue(all(c.chars <= hard * 1.5 for c in chunks))


class TestIndexAndQuery(Base):
    def _build_all(self):
        from wikilib import cli
        cli.main(["--config", self.cfg_path, "build"])

    def test_two_collections_coexist(self):
        """连续 build 两个 collection，不能互相删库。"""
        self.write("a.md", CLIP)
        with open(self.cfg_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw["collections"].append({"name": "docs2", "label": "第二组",
                                   "sources": [self.src2]})
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
        self.cfg = config.load(self.cfg_path)
        self.write("b.md", PLAIN, where=self.src2)

        from wikilib import cli
        cli.main(["--config", self.cfg_path, "build", "--collection", "notes"])
        cli.main(["--config", self.cfg_path, "build", "--collection", "docs2"])

        conn = index.connect(self.cfg.index_db)
        cols = {r[0] for r in conn.execute("SELECT DISTINCT collection FROM docs")}
        conn.close()
        self.assertEqual(cols, {"notes", "docs2"})

    def test_alias_and_anchor_searchable(self):
        """别名与引用锚点都要能被检索到。"""
        self.write("a.md", CLIP)
        conn = index.connect(self.cfg.index_db, create=True)
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        chunks = chunk.chunk_collection(rep.docs, self.cfg)
        index.build(conn, "notes", rep.docs, chunks)
        page = {"slug": "p", "title": "混淆概念", "type": "concept",
                "status": "active", "aliases": ["Confused Deputy"],
                "cites": [], "body": "# 混淆概念\n\n## 定义与边界\n\n【[[剪藏文章#New Attack Surface]]】\n",
                "rel_path": "wiki/notes/concepts/p.md"}
        index.index_pages(conn, "notes", [page])

        hit = query.search_pages(conn, "confused deputy", collection="notes")
        self.assertTrue(hit and hit[0]["title"] == "混淆概念")

        hit = query.search_pages(conn, "New Attack Surface", collection="notes")
        self.assertTrue(hit, "引用锚点里的文本应当可检索")
        conn.close()

    def test_note_flagged(self):
        conn = index.connect(self.cfg.index_db, create=True)
        index.index_pages(conn, "notes", [{
            "slug": "n", "title": "一条判断", "type": "note", "status": "draft",
            "aliases": [], "cites": [], "body": "# 一条判断\n\n## 背景\n\n内容\n",
            "rel_path": "wiki/notes/notes/n.md"}])
        res = query.search_pages(conn, "一条判断", collection="notes")
        self.assertTrue(res[0]["uncorroborated"])
        conn.close()


class TestVerify(Base):
    def _page(self, text):
        path = os.path.join(self.wiki, "notes", "concepts", "x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return contracts.parse_page(path)

    def _ctx(self):
        from wikilib import glossary
        snap = verify.Snapshot()
        return snap, glossary.Glossary()

    def test_note_passes_without_seven_sections(self):
        page = self._page("""---
title: 一条判断
slug: n
type: note
collection: notes
status: draft
origin: conversation
compiled_by: human
compiled_at: 2026-11-06T10:00:00
language: zh
sources: []
---
# 一条判断

## 背景

内容。

## 结论

内容。
""")
        snap, gl = self._ctx()
        res = verify.verify_page(page, snap, gl, 3000, 0.45)
        self.assertEqual([r for r in res if r.severity == "error"], [])

    def test_note_requires_origin(self):
        page = self._page("""---
title: 缺 origin
slug: n2
type: note
collection: notes
status: draft
compiled_by: human
compiled_at: 2026-11-06T10:00:00
language: zh
sources: []
---
# 缺 origin

## 背景

内容。
""")
        snap, gl = self._ctx()
        rules = {r.rule for r in verify.verify_page(page, snap, gl, 3000, 0.45)}
        self.assertIn("meta_missing", rules)

    def test_structured_page_needs_citation(self):
        page = self._page("""---
title: 缺引用
slug: c1
type: concept
collection: notes
status: active
compiled_by: x
compiled_at: 2026-11-06T10:00:00
language: zh
sources:
  - doc: d
    cites: []
---
# 缺引用

## 定义与边界

没有引用的断言。

## 关键构成

x

## 在本知识库中的证据

x

## 与其他页的关系

- [[a]]

## 争议与不同说法

x

## 适用范围与局限

x

## 来源索引

- [[d]]
""")
        snap, gl = self._ctx()
        rules = {r.rule for r in verify.verify_page(page, snap, gl, 3000, 0.45)}
        self.assertIn("section_no_citation", rules)


class TestPlanAndUsage(Base):
    def test_triage_uses_containment(self):
        page = {"slug": "hitl", "title": "HITL", "aliases": ["Human-in-the-Loop"]}
        st = state.SourceState(doc_id="Human-in-the-loop Agent 设计：哪些步骤必须人工审批",
                               rel_path="x.md", sha256="h",
                               headings=[{"norm": "背景", "orig": "背景"}])
        hits = plan.triage_new(st.doc_id, [page], {}, st)
        self.assertEqual(hits[0].page, "hitl", "包含匹配应当命中")

    def test_usage_import_is_idempotent(self):
        """同一会话导入两次不能翻倍——用假 record 直接验增量逻辑。"""
        usage.record(self.cfg, usage.new_record(
            "session", "", subject="s", actual_in=100, actual_out=50,
            actual_cache_read=0, session_id="abc"))
        recs = [r for r in usage.load_all(self.cfg) if r.session_id == "abc"]
        self.assertEqual(sum(r.actual_in for r in recs), 100)

    def test_usage_kind_separation(self):
        """开发 wiki 的消耗不能混进内容产出。"""
        usage.record(self.cfg, usage.new_record(
            "build", "notes", subject="编译", est_in=100, est_out=50))
        usage.record(self.cfg, usage.new_record(
            "session", "", subject="开发", actual_in=1000, actual_out=500,
            session_id="dev1", kind=usage.KIND_DEV))
        recs = usage.load_all(self.cfg)
        content = usage.summary(recs, kind=usage.KIND_CONTENT)
        dev = usage.summary(recs, kind=usage.KIND_DEV)
        self.assertEqual(content["est_total"], 150)
        self.assertEqual(content["actual_total"], 0)
        self.assertEqual(dev["actual_total"], 1500)

    def test_cursor_roundtrip(self):
        usage.save_cursor(self.cfg, "abc", {"input_tokens": 10,
                                            "output_tokens": 5,
                                            "cache_read_input_tokens": 0})
        cur = usage.load_cursor(self.cfg)
        self.assertEqual(cur["abc"]["input_tokens"], 10)

    def test_config_rejects_paths_outside_vault(self):
        with self.assertRaises(config.SourcePathError):
            config.validate_source_path(self.cfg, "/tmp")
        with self.assertRaises(config.SourcePathError):
            config.validate_source_path(self.cfg, self.vault)          # vault 根
        with self.assertRaises(config.SourcePathError):
            config.validate_source_path(self.cfg, self.wiki)           # wiki 根
        self.assertEqual(config.validate_source_path(self.cfg, self.src),
                         os.path.realpath(self.src))


if __name__ == "__main__":
    unittest.main()
