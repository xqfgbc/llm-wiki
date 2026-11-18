"""术语表：中英唯一译法 + 查询扩展来源。

这个模块现在是 `lexicon` 的查询视图：
  * `Glossary.forms`  —— 中文 → 全部写法（中文/英文/缩写/全称），供 BM25 扩展
  * `Glossary.banned` —— 已知劣质译法
  * `Glossary.keep_en` —— 保留英文的专名

词表本体（概念 / 专有名词 / 缩写）在 `wikilib/lexicon.py`。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import lexicon as lexicon_mod
from .lexicon import normalize_term
from .tokenize import query_terms

_CJK = re.compile(r"[一-鿿]")

MAX_EXPANSION = 40


@dataclass
class Glossary:
    forms: Dict[str, List[str]] = field(default_factory=dict)   # 规范中文 -> 全部写法
    banned: List[str] = field(default_factory=list)
    keep_en: List[str] = field(default_factory=list)
    lexicon: lexicon_mod.Lexicon = field(default_factory=lexicon_mod.Lexicon)

    def expand(self, raw_query: str, terms: List[str]) -> List[str]:
        """把中文提问扩成「中文 + 对应英文」。

        两层，先精确后宽松：
        A. 术语整词出现在提问里（长词优先）→ 把该术语的全部写法加进来
        B. 提问里的短中文 token 是某个术语的前缀 → 只有一个候选就全加；
           有多个候选（如「令牌」对应 令牌库/令牌透传/令牌交换/安全令牌服务），
           只加它们的**英文词交集**，避免把一个词炸成四个不相干的概念
        """
        out = list(dict.fromkeys(terms))
        seen = set(out)
        matched_zh: List[str] = []

        def push(text: str) -> None:
            for tok in query_terms(text):
                if tok not in seen and len(out) < MAX_EXPANSION:
                    seen.add(tok)
                    out.append(tok)

        # A. 整词命中
        for zh in sorted(self.forms, key=len, reverse=True):
            if len(zh) >= 2 and zh in raw_query:
                matched_zh.append(zh)
                for form in self.forms[zh]:
                    push(form)

        # B. 前缀/包含命中
        for term in terms:
            if not _CJK.search(term) or len(term) < 2:
                continue
            if any(term in zh for zh in matched_zh):
                continue
            cands = [zh for zh in self.forms
                     if len(zh) >= 2 and (term in zh or zh in term)]
            if not cands:
                continue
            if len(cands) == 1:
                for form in self.forms[cands[0]]:
                    push(form)
                continue
            # 只对**英文** token 求交：中英文 token 本来就不同，
            # 混在一起求交必然为空（凭证代理 vs Credential Brokering 没有一个共同 token）。
            sets = []
            for zh in cands:
                for form in self.forms[zh]:
                    ascii_toks = {t for t in query_terms(form) if not _CJK.search(t)}
                    if ascii_toks:
                        sets.append(ascii_toks)
            common = set.intersection(*sets) if sets else set()
            for tok in sorted(common):
                if tok not in seen and len(out) < MAX_EXPANSION:
                    seen.add(tok)
                    out.append(tok)
        return out


def _forms_of(e: lexicon_mod.LexEntry) -> List[str]:
    """查询扩展用的写法：中文 + 英文 + 缩写全称。"""
    out: List[str] = []
    for x in [e.zh, e.canonical, e.expansion] + list(e.aliases):
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def from_lexicon(lex: lexicon_mod.Lexicon) -> Glossary:
    g = Glossary(lexicon=lex)
    g.banned = list(lex.banned)
    g.keep_en = list(lex.keep_en)
    for e in lex.all_entries():
        forms = _forms_of(e)
        # 有中文的，中文作为键；没有中文的（纯缩写/专名），用 canonical 自身作键
        key = e.zh or e.canonical
        if not key:
            continue
        merged = g.forms.setdefault(key, [])
        for f in forms:
            if f not in merged:
                merged.append(f)
    return g


def load(path: str) -> Glossary:
    return from_lexicon(lexicon_mod.load(path))


def load_lexicon(path: str) -> lexicon_mod.Lexicon:
    return lexicon_mod.load(path)


def load_for(cfg) -> Glossary:
    return load(os.path.join(cfg.obsidian.wiki_root, "GLOSSARY.md"))


def lexicon_for(cfg) -> lexicon_mod.Lexicon:
    return lexicon_mod.load(os.path.join(cfg.obsidian.wiki_root, "GLOSSARY.md"))
