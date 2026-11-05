"""术语表：中英唯一译法 + 查询扩展来源。

为什么查询要做术语扩展
----------------------
试点语料 8 篇里 6 篇是英文，而提问基本是中文。BM25 是词法检索，
中文「凭证」在英文正文里一个字符都匹配不到，直接问会全篇落空。
所以把 `GLOSSARY.md` 的中英对照拿来做查询扩展：命中中文就补上英文写法。

扩展只加召回，不改排序语义；`normalize_term` 与 AI-KG 同口径（去分隔符、小写），
这样 `Token Vault` / `token-vault` / `TokenVault` 会被认成同一个词。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .tokenize import query_terms

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
_SEP_ROW = re.compile(r"^\|[\s:\-|]+\|$")

MAX_EXPANSION = 40


def normalize_term(text: str) -> str:
    """去分隔符 + 小写。与 AI-KG 的 `kb/normalize.normalize_term` 同口径。"""
    return re.sub(r"[\s\-_·・]+", "", (text or "")).strip().lower()


@dataclass
class Glossary:
    forms: Dict[str, List[str]] = field(default_factory=dict)   # 规范中文 -> 全部写法
    banned: List[str] = field(default_factory=list)
    keep_en: List[str] = field(default_factory=list)

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


def _clean_cell(cell: str) -> str:
    return cell.strip().strip("`").strip()


def load(path: str) -> Glossary:
    g = Glossary()
    if not os.path.exists(path):
        return g

    section = ""
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s.startswith("##"):
            section = s.lstrip("#").strip()
            continue
        if section.startswith("保留英文"):
            for item in re.split(r"[·、,，]", s):
                item = item.strip()
                if item:
                    g.keep_en.append(item)
            continue
        m = _TABLE_ROW.match(s)
        if not m or _SEP_ROW.match(s):
            continue
        cells = [_clean_cell(c) for c in m.group(1).split("|")]
        if len(cells) < 2 or cells[0] in ("英文", "---"):
            continue
        en, zh = cells[0], cells[1]
        if not en or not zh or en == "英文":
            continue
        forms = [zh, en]
        if len(cells) >= 3 and cells[2] and cells[2] != "—":
            g.forms.setdefault(zh, forms)
        else:
            g.forms.setdefault(zh, forms)
        # 禁用译法单列收集
        if len(cells) >= 3 and cells[2] and cells[2] != "—":
            for bad in re.split(r"[、,，]", cells[2]):
                bad = bad.strip()
                if bad:
                    g.banned.append(bad)
    return g


def load_for(cfg) -> Glossary:
    return load(os.path.join(cfg.obsidian.wiki_root, "GLOSSARY.md"))
