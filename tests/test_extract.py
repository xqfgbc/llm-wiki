"""P1 抽取 / 对抗复核 / 词汇表的回归测试。"""
import json
import os
import shutil
import tempfile
import unittest

import yaml

from wikilib import (config, contracts, exclusions, extract, glossary, human_html,
                     lexicon, normalize, plan, registry, review, verify)

GLOSSARY = """# 术语表

## 专有名词

| 名称 | 类型 | 中文/说明 | 别名 |
|---|---|---|---|
| VPC | service | 虚拟私有云 | Virtual Private Cloud |
| NAT Gateway | service | NAT 网关 | NAT 网关 |
| Istio | product | 服务网格 | Istio Service Mesh |
| Istio Ambient | mode | Ambient 模式 | Ambient 模式 |
| ztunnel | component | L4 代理 | — |

## 缩写

| 缩写 | 全称 | 中文 | 说明 |
|---|---|---|---|
| VPC | Virtual Private Cloud | 虚拟私有云 | — |

## 通用缩写（不建页）

| 缩写 | 全称 | 中文 | 说明 |
|---|---|---|---|
| SSO | Single Sign-On | 单点登录 | 通用缩写，不建页 |
"""

DOC1 = """---
title: 技术架构笔记
---
## VPC 与 NAT Gateway

VPC 是虚拟私有云。NAT Gateway 是 VPC 的出口组件，负责私有子网访问公网。SSO 登录位于入口。

## Istio Ambient

Istio 是服务网格。Ambient 模式下，ztunnel 是 L4 代理。ztunnel 负责 mTLS。
"""

DOC2 = """---
title: 网络补充
---
## VPC 路由

VPC 的路由表决定子网流量。NAT Gateway 负责出站。ztunnel 不处理 L7。
"""

PAGE = """---
title: Istio Ambient
slug: istio-ambient
type: entity
collection: notes
status: active
compiled_by: test
compiled_at: 2026-11-18T00:00:00
language: zh
aliases: [Istio, Ambient 模式]
sources: []
---
# Istio Ambient

> 一句话。

## 定义与边界

正文。
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
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "version": 1,
                "obsidian": {"vault_root": self.vault, "vault_name": "vault",
                             "wiki_root": self.wiki},
                "data_dir": os.path.join(self.tmp, "data"),
                "collections": [{"name": "notes", "label": "笔记",
                                 "sources": [self.src]}],
            }, f, allow_unicode=True)
        self.cfg = config.load(self.cfg_path)
        with open(os.path.join(self.wiki, "GLOSSARY.md"), "w", encoding="utf-8") as f:
            f.write(GLOSSARY)
        self.write("a.md", DOC1)
        self.write("b.md", DOC2)
        page_dir = os.path.join(self.wiki, "notes", "entities")
        os.makedirs(page_dir)
        with open(os.path.join(page_dir, "istio-ambient.md"), "w",
                  encoding="utf-8") as f:
            f.write(PAGE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.src, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def candidates(self):
        ex = exclusions.load(self.wiki)
        rep = normalize.scan_collection(self.cfg.collections[0], self.cfg, ex)
        lex = glossary.lexicon_for(self.cfg)
        cands = extract.generate_candidates(rep.docs, lex)
        pages = verify.load_pages(os.path.join(self.wiki, "notes"))
        reg = registry.load(os.path.join(self.wiki, "entities.yaml"))
        extract.apply_gates(cands, extract.index_pages(pages), reg, lex)
        return cands


class TestLexicon(Base):
    def test_merge_proper_and_acronym(self):
        lex = lexicon.load(os.path.join(self.wiki, "GLOSSARY.md"))
        vpc = lex.find("VPC")
        self.assertIsNotNone(vpc)
        self.assertEqual(vpc.canonical, "VPC")
        self.assertEqual(vpc.expansion, "Virtual Private Cloud")
        # 专有名词里的别名不能被缩写表再建一条重复条目
        self.assertEqual(len([e for e in lex.all_entries()
                              if e.canonical.lower() == "vpc"]), 1)

    def test_add_entry_roundtrip(self):
        path = os.path.join(self.wiki, "GLOSSARY.md")
        lexicon.add_entry(path, canonical="ALB", kind="acronym",
                          expansion="Application Load Balancer", zh="应用负载均衡器")
        lex = lexicon.load(path)
        e = lex.find("ALB")
        self.assertEqual(e.expansion, "Application Load Balancer")
        self.assertIn("应用负载均衡器", e.zh)


class TestExtract(Base):
    def test_captures_word_proper_nouns(self):
        cands = self.candidates()
        for key in ("vpc", "natgateway", "istio", "ztunnel"):
            self.assertIn(key, cands, "缺少候选：%s" % key)
        self.assertGreaterEqual(cands["natgateway"].mentions, 1)
        self.assertTrue(cands["natgateway"].evidence)
        self.assertTrue(cands["ztunnel"].evidence[0].quote)

    def test_alias_hypernym_goes_to_review(self):
        cands = self.candidates()
        istio = cands["istio"]
        self.assertEqual(istio.gate, "needs_review")
        self.assertTrue(istio.gate_reason.startswith("alias_hypernym"))

    def test_common_abbreviation_not_a_page_candidate(self):
        cands = self.candidates()
        self.assertEqual(cands["sso"].gate, "observation")
        self.assertEqual(cands["sso"].gate_reason, "common_abbreviation")

    def test_existing_page_is_covered(self):
        cands = self.candidates()
        ambient = cands["istioambient"]
        self.assertEqual(ambient.gate, "covered")
        self.assertEqual(ambient.existing_page, "istio-ambient")


class TestReview(Base):
    def _candidate(self, surface):
        cands = self.candidates()
        key = lexicon.normalize_term(surface)
        return cands[key].as_dict(), cands

    def test_missing_challenge_escalates(self):
        c, _ = self._candidate("NAT Gateway")
        res = review.resolve(
            [{"candidate_id": c["candidate_id"], "action": "accept_new",
              "canonical": "NAT Gateway", "type": "entity", "confidence": 0.9}],
            [], [], {c["candidate_id"]: c})
        self.assertEqual(res[0].human_reason, "missing_challenge")

    def test_blocker_escalates(self):
        c, _ = self._candidate("NAT Gateway")
        res = review.resolve(
            [{"candidate_id": c["candidate_id"], "action": "accept_new",
              "canonical": "NAT Gateway", "type": "entity", "confidence": 0.9}],
            [{"candidate_id": c["candidate_id"], "verdict": "uphold",
              "objections": [{"rule": "C", "severity": "blocker",
                              "evidence": "它是 VPC 的出口组件"}]}],
            [], {c["candidate_id"]: c})
        self.assertEqual(res[0].human_reason, "unresolved_blocker")

    def test_apply_creates_registry_and_order(self):
        c, cands = self._candidate("NAT Gateway")
        res = review.resolve(
            [{"candidate_id": c["candidate_id"], "action": "accept_new",
              "canonical": "NAT Gateway", "type": "entity",
              "aliases": ["NAT 网关"], "confidence": 0.9}],
            [{"candidate_id": c["candidate_id"], "verdict": "uphold",
              "objections": []}],
            [], {c["candidate_id"]: c})
        stats = review.apply_resolutions(
            self.cfg, "notes", res, {c["candidate_id"]: c})
        self.assertEqual(stats["orders"], 1)
        self.assertTrue(os.path.exists(stats["registry_path"]))
        reg = registry.load(stats["registry_path"])
        self.assertIsNotNone(reg.find("NAT Gateway"))
        self.assertTrue(os.path.exists(stats["order_paths"][0]))

    def test_enrich_orders_adds_evidence_packet(self):
        odir = os.path.join(self.tmp, "data", "orders", "notes")
        edir = os.path.join(odir, "extract")
        rdir = os.path.join(self.tmp, "data", "review", "notes")
        os.makedirs(edir)
        os.makedirs(rdir)
        with open(os.path.join(edir, "_candidates.json"), "w", encoding="utf-8") as f:
            json.dump({"collection": "notes", "candidates": [{
                "candidate_id": "natgateway:abc123",
                "surface": "NAT Gateway",
                "key": "natgateway",
                "proposed": {"canonical": "NAT Gateway"},
                "evidence": [{"doc": "架构", "doc_id": "架构", "heading": "VPC",
                              "quote": "NAT Gateway 是出口组件", "offset": 1,
                              "definitional": True}],
            }]}, f, ensure_ascii=False)
        with open(os.path.join(rdir, "_resolved.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        order = {
            "slug": "nat-gateway", "mode": "new",
            "frontmatter": {"title": "NAT Gateway"},
            "entity_review": {"adversarial": True},
            "sources": [],
        }
        with open(os.path.join(odir, "nat-gateway.json"), "w", encoding="utf-8") as f:
            json.dump(order, f, ensure_ascii=False)
        stats = review.enrich_orders(self.cfg, "notes")
        self.assertEqual(stats["enriched"], 1)
        saved = json.load(open(os.path.join(odir, "nat-gateway.json"), encoding="utf-8"))
        self.assertEqual(saved["evidence"][0]["quote"], "NAT Gateway 是出口组件")
        self.assertEqual(saved["sources"][0]["doc_id"], "架构")
        self.assertEqual(saved["sources"][0]["headings"][0]["norm"], "VPC")

    def test_update_order_gets_evidence_packet(self):
        rawdir = os.path.join(self.tmp, "data", "raw", "notes")
        os.makedirs(rawdir)
        with open(os.path.join(rawdir, "doc1.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitle: 架构笔记\n---\n\n## VPC\n\n"
                    "VPC 是虚拟私有云。它定义 IP 段并划分子网。\n")
        order = {
            "slug": "vpc",
            "mode": "recompile",
            "sources": [{"doc_id": "doc1",
                         "headings": [{"norm": "VPC", "orig": "VPC"}]}],
        }
        plan.attach_evidence(order, rawdir)
        self.assertTrue(order["evidence"])
        self.assertEqual(order["evidence"][0]["heading"], "VPC")
        self.assertIn("虚拟私有云", order["evidence"][0]["quote"])
        self.assertEqual(order["evidence"][0]["collection"], "notes")

    def test_human_review_html_is_generated(self):
        qdir = os.path.join(self.tmp, "data", "review", "notes")
        os.makedirs(qdir)
        with open(os.path.join(qdir, "_human_queue.json"), "w", encoding="utf-8") as f:
            json.dump({
                "collection": "notes",
                "count": 1,
                "groups": {"unresolved_blocker": [{
                    "candidate_id": "natgateway:abc123",
                    "surface": "NAT Gateway",
                    "action": "accept_new",
                    "canonical": "NAT Gateway",
                    "type": "entity",
                    "aliases": ["NAT 网关"],
                    "merge_into": "",
                    "qualifier": "",
                    "confidence": 0.8,
                    "objections": [{"rule": "C", "severity": "blocker",
                                    "evidence": "它是 VPC 的出口组件", "fix": "并入 VPC"}],
                    "evidence": [{"doc": "架构", "heading": "VPC",
                                  "quote": "NAT Gateway 是出口组件"}],
                    "human_reason": "unresolved_blocker",
                    "human_options": ["新建", "并入"],
                }]},
            }, f, ensure_ascii=False)
        out = os.path.join(self.tmp, "human_review.html")
        path = human_html.build(self.cfg, out_path=out)
        text = open(path, encoding="utf-8").read()
        self.assertIn("Wiki 人审工作台", text)
        self.assertIn("NAT Gateway", text)
        self.assertIn("notes::natgateway:abc123", text)
        self.assertNotIn("__DATA__", text)

    def test_defer_writes_human_queue(self):
        c, _ = self._candidate("NAT Gateway")
        res = review.resolve(
            [{"candidate_id": c["candidate_id"], "action": "accept_new",
              "canonical": "NAT Gateway", "type": "entity", "confidence": 0.9}],
            [{"candidate_id": c["candidate_id"], "verdict": "defer",
              "objections": []}],
            [], {c["candidate_id"]: c})
        stats = review.apply_resolutions(
            self.cfg, "notes", res, {c["candidate_id"]: c})
        self.assertEqual(stats["human"], 1)
        self.assertTrue(os.path.exists(stats["human_queue"]["path"]))
        text = open(stats["human_queue"]["path"], encoding="utf-8").read()
        self.assertIn("NAT Gateway", text)


if __name__ == "__main__":
    unittest.main()
