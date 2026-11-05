"""jieba 预分词 —— FTS5 的 unicode61 分词器不切中文，必须先把中文切成空格分隔的 token。

⚠️ 这套规则与 AI-KG 的 `kg-pipeline/kb/tokenize.py` 保持一致，但**独立实现**：
两库零 import 是硬约束。改这里的规则时，若要保持两库检索口径可比，
应同步修改 AI-KG 侧并各自跑一遍评测。
"""
from __future__ import annotations

import re
import warnings
from typing import Iterable, List

# jieba 0.42 的正则里有非转义反斜杠，Python 3.12 会刷一屏 SyntaxWarning。
# 它只是源码写法问题，不影响分词结果，这里静音。
warnings.filterwarnings("ignore", category=SyntaxWarning, module="jieba")

import logging  # noqa: E402

import jieba  # noqa: E402

jieba.setLogLevel(logging.ERROR)

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-/&\._]*")
_CJK = re.compile(r"[一-鿿]")
_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def _keep(token: str) -> bool:
    token = token.strip()
    if not token or _PUNCT_ONLY.match(token):
        return False
    if len(token) == 1 and _CJK.match(token):
        return False
    return True


def strip_wikilinks(text: str) -> str:
    """`[[文档#标题]]` → `文档`。锚点文本进索引会污染标题词的权重。"""
    return WIKILINK.sub(r"\1", text or "")


def tokens(text: str) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    seen = set()
    for tok in jieba.cut(text):
        tok = tok.strip()
        if not _keep(tok):
            continue
        for sub in (_ASCII_WORD.findall(tok) or [tok]):
            sub = sub.lower().strip()
            if _keep(sub) and sub not in seen:
                seen.add(sub)
                out.append(sub)
    return out


def to_index_text(text: str) -> str:
    return " ".join(tokens(strip_wikilinks(text)))


def query_terms(query: str) -> List[str]:
    return tokens(query)


def quote_fts(term: str) -> str:
    return '"%s"' % term.replace('"', '""')


def build_match_expr(terms: Iterable[str], operator: str = " OR ") -> str:
    """FTS5 对空格分隔的词默认取 AND，长查询会几乎全空；一律用 OR，让 bm25 排序。"""
    parts = [quote_fts(t) for t in dict.fromkeys(terms) if t and len(t) >= 1]
    return operator.join(parts)
