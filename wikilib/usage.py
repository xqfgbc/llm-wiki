"""Token 用量观测。

能测什么、不能测什么
-------------------
管道本身**不调用大模型**——撰写由 Claude Code 完成。所以这里测的是
「交给模型的文本量」与「模型产出的文本量」，再按固定系数折算成 token 估算值。

- `est_*`：确定性估算，每次运行都会产生，可累积、可比较
- `actual_*`：真实用量，只能从 Claude Code 的会话记录里读出来，
  由 `llm-wiki usage import` 导入；没有就是 null

估算系数（保守取整）：
- 中文 1 字 ≈ 0.9 token
- 其余字符 ≈ 0.25 token（英文约 4 字符/token）

估算**不含**模型自身的推理开销、系统提示、工具往返，因此真实值通常更高。
页面上把两者分开显示，不混为一谈。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

CJK = re.compile(r"[一-鿿]")
CJK_RATIO = 0.9
OTHER_RATIO = 0.25


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(CJK.findall(text))
    other = len(text) - cjk
    return int(cjk * CJK_RATIO + other * OTHER_RATIO)


def path_for(cfg) -> str:
    return os.path.join(cfg.data_dir, "usage.jsonl")


@dataclass
class Record:
    ts: str
    op: str                       # update | build | session
    collection: str
    subject: str = ""
    pages: List[str] = field(default_factory=list)
    chars_in: int = 0
    chars_out: int = 0
    est_in: int = 0
    est_out: int = 0
    actual_in: Optional[int] = None
    actual_out: Optional[int] = None
    actual_cache_read: Optional[int] = None
    session_id: Optional[str] = None
    note: str = ""

    @property
    def est_total(self) -> int:
        return self.est_in + self.est_out

    @property
    def actual_total(self) -> Optional[int]:
        if self.actual_in is None and self.actual_out is None:
            return None
        return (self.actual_in or 0) + (self.actual_out or 0)


def record(cfg, rec: Record) -> str:
    path = path_for(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
    return path


def load_all(cfg) -> List[Record]:
    path = path_for(cfg)
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Record(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def summary(records: List[Record]) -> Dict:
    est_in = sum(r.est_in for r in records)
    est_out = sum(r.est_out for r in records)
    act_in = sum(r.actual_in or 0 for r in records)
    act_out = sum(r.actual_out or 0 for r in records)
    cache = sum(r.actual_cache_read or 0 for r in records)
    return {
        "ops": len(records),
        "est_in": est_in,
        "est_out": est_out,
        "est_total": est_in + est_out,
        "actual_in": act_in,
        "actual_out": act_out,
        "actual_total": act_in + act_out,
        "actual_cache_read": cache,
        "pages": sum(len(r.pages) for r in records),
    }


def new_record(op: str, collection: str, **kw) -> Record:
    return Record(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), op=op,
                  collection=collection, **kw)


# ---------------------------------------------------------------- 实际用量导入
def scan_transcripts(project_dir: str,
                     session_id: Optional[str] = None) -> Dict[str, int]:
    """从 Claude Code 的会话记录汇总真实用量。

    `project_dir` 形如 `~/.claude/projects/-Users-...-AI--`；
    不传 `session_id` 时取该目录下最新的一份 jsonl。
    """
    import glob
    files: List[str] = []
    if session_id:
        root = os.path.join(project_dir, session_id)
        files = [root + ".jsonl"] + sorted(glob.glob(root + "/subagents/*.jsonl"))
    else:
        cands = sorted(glob.glob(os.path.join(project_dir, "*.jsonl")),
                       key=os.path.getmtime)
        if not cands:
            return {}
        newest = cands[-1]
        # 子代理的记录放在 <session>/subagents/ 下，必须一并统计，
        # 否则并行编译的消耗会被漏掉（实测能漏掉三分之一）。
        stem = os.path.splitext(os.path.basename(newest))[0]
        files = [newest] + sorted(glob.glob(
            os.path.join(project_dir, stem, "subagents", "*.jsonl")))

    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "calls": 0}
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = d.get("message") if isinstance(d.get("message"), dict) else None
                u = (m or {}).get("usage") or d.get("usage")
                if not isinstance(u, dict):
                    continue
                total["calls"] += 1
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens"):
                    total[k] += u.get(k, 0) or 0
    return total


def import_session(cfg, project_dir: str, session_id: Optional[str] = None) -> Optional[Dict]:
    """导入一个会话的**增量**用量，重复导入不会重复计数。

    会话记录里的数字是该会话的累计值，不是增量。所以要拿
    「当前累计 − 该会话已导入过的量」再记录；差值为零就跳过。
    """
    import glob
    if not session_id:
        cands = glob.glob(os.path.join(os.path.expanduser(project_dir), "*.jsonl"))
        if not cands:
            return None
        newest = max(cands, key=os.path.getmtime)
        session_id = os.path.splitext(os.path.basename(newest))[0]

    totals = scan_transcripts(project_dir, session_id)
    if not totals:
        return None

    prior = [r for r in load_all(cfg) if r.session_id == session_id]
    seen_in = sum(r.actual_in or 0 for r in prior)
    seen_out = sum(r.actual_out or 0 for r in prior)
    seen_cache = sum(r.actual_cache_read or 0 for r in prior)

    delta = {
        "input_tokens": totals["input_tokens"] - seen_in,
        "output_tokens": totals["output_tokens"] - seen_out,
        "cache_read_input_tokens": totals["cache_read_input_tokens"] - seen_cache,
        "calls": totals["calls"],
    }
    if delta["input_tokens"] <= 0 and delta["output_tokens"] <= 0:
        return {"session_id": session_id, "skipped": True, "totals": totals}

    record(cfg, new_record(
        "session", "",
        subject="会话 %s（%d 次调用）" % (session_id[:8], totals["calls"]),
        actual_in=delta["input_tokens"], actual_out=delta["output_tokens"],
        actual_cache_read=delta["cache_read_input_tokens"],
        session_id=session_id,
        note="增量导入：当前累计减去该会话已记录的量"))
    delta["session_id"] = session_id
    delta["skipped"] = False
    return delta


def format_int(n: Optional[int]) -> str:
    return "—" if n is None else format(n, ",")
