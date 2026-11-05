"""排除名单：四级粒度（file / glob / heading / term）。

设计要点
--------
1. **两份文件，互不覆盖**
   - `EXCLUSIONS.yaml`      人工维护（可在 Obsidian 里直接编辑），代码只读
   - `EXCLUSIONS.auto.yaml` CLI `add` 写入，代码读写
   这样手写的注释不会被 CLI 重写冲掉。

2. **每条必须带 reason**。缺失直接报错——排除项没有理由，半年后没人敢动。

3. **指纹**：file 级排除记录 sha256。被排除的文件变了，`recheck` 会提示复核
   （可能是修好了，也可能是换成了别的内容）。

4. **悬空检测**：`canonical` 指向的文件不存在时报告，提示名单已过期。
"""
from __future__ import annotations

import datetime
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import yaml

EXCLUDED_NAME = "EXCLUSIONS.yaml"
AUTO_NAME = "EXCLUSIONS.auto.yaml"

HEADER = """# 排除名单 —— 人工维护，代码只读
#
# 四级粒度，每条必须有 reason：
#   files:    整份文件排除（最常用；重复件、机翻件）
#   globs:    按通配符排除
#   headings: 章节级排除（网页剪藏的「目录 / Contents / 相关文章」）
#   terms:    不建页的术语（「致谢」「联系人」这类结构性词）
#
# 由 CLI 追加的条目写在 EXCLUSIONS.auto.yaml，两份在运行时合并。

version: 1

defaults:
  headings: []
  globs: []
  terms: []

collections: {}
"""


class ExclusionError(RuntimeError):
    pass


@dataclass
class FileRule:
    path: str
    reason: str
    canonical: str = ""
    added: str = ""
    sha256: str = ""
    source_file: str = ""


@dataclass
class Issue:
    kind: str                      # changed | dangling | missing
    message: str
    severity: str = "warn"


@dataclass
class Scope:
    """单个 collection 的排除规则；全局规则单独一份。"""
    headings: List[str] = field(default_factory=list)
    globs: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)
    files: Dict[str, FileRule] = field(default_factory=dict)

    def merge(self, other: "Scope") -> None:
        self.headings.extend(other.headings)
        self.globs.extend(other.globs)
        self.terms.extend(other.terms)
        self.files.update(other.files)


@dataclass
class Exclusions:
    wiki_root: str
    global_scope: Scope = field(default_factory=Scope)
    scopes: Dict[str, Scope] = field(default_factory=dict)

    def scope_for(self, collection: str) -> Scope:
        return self.scopes.get(collection, Scope())

    # ---------------------------------------------------------------- 判定
    def match_file(self, collection: str, rel_path: str) -> Optional[FileRule]:
        rel = rel_path.replace(os.sep, "/")
        base = os.path.basename(rel)
        scope = self.scope_for(collection)
        for key in (rel, base):
            if key in scope.files:
                return scope.files[key]
        for pat in list(self.global_scope.globs) + scope.globs:
            # `**/x` 在 fnmatch 里要求有斜杠，而顶层文件的 rel 不含斜杠，
            # 所以同时用去掉前缀的变体再试一次。
            variants = {pat}
            if pat.startswith("**/"):
                variants.add(pat[3:])
            for v in variants:
                if fnmatch.fnmatch(rel, v) or fnmatch.fnmatch(base, v):
                    return FileRule(path=rel, reason="glob: %s" % pat)
        return None

    def match_heading(self, collection: str, heading: str) -> Optional[str]:
        norm = _norm(heading)
        for h in list(self.global_scope.headings) + self.scope_for(collection).headings:
            if _norm(h) == norm:
                return "排除章节：%s" % h
        return None

    def match_term(self, collection: str, term: str) -> Optional[str]:
        norm = _norm(term)
        for t in list(self.global_scope.terms) + self.scope_for(collection).terms:
            if _norm(t) == norm:
                return "排除术语：%s" % t
        return None

    # ---------------------------------------------------------------- 自检
    def recheck(self, collection: str, source_root: str) -> List[Issue]:
        issues: List[Issue] = []
        rules = dict(self.scope_for(collection).files)
        for path, rule in rules.items():
            abs_path = os.path.join(source_root, path)
            if not os.path.exists(abs_path):
                issues.append(Issue("missing", "排除项指向的文件已不存在：%s" % path))
                continue
            if rule.sha256 and sha256_file(abs_path) != rule.sha256:
                issues.append(Issue(
                    "changed", "被排除的文件已变更，请复核是否仍需排除：%s" % path))
            if rule.canonical:
                if not os.path.exists(os.path.join(source_root, rule.canonical)):
                    issues.append(Issue(
                        "dangling",
                        "排除项的 canonical 不存在（名单可能过期）：%s" % rule.canonical))
        return issues


# -------------------------------------------------------------------- 内部
def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).strip().lower()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _read(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse(data: Dict[str, Any], source_file: str) -> tuple[Scope, Dict[str, Scope]]:
    """把一份 yaml 解析成 (全局 scope, {collection: scope})。"""
    glob = Scope()

    def take_scope(raw: Dict[str, Any], target: Scope, collection: Optional[str]) -> None:
        for key in ("headings", "globs", "terms"):
            for item in raw.get(key) or []:
                if str(item).strip():
                    getattr(target, key).append(str(item))
        for entry in raw.get("files") or []:
            if collection is None:
                raise ExclusionError(
                    "%s: files 必须写在具体 collection 下，不能写在 defaults 里"
                    % source_file)
            if isinstance(entry, str):
                raise ExclusionError(
                    "%s: collections.%s.files 的条目必须写成 {path, reason}：%s"
                    % (source_file, collection, entry))
            if not isinstance(entry, dict) or not entry.get("path"):
                raise ExclusionError("%s: 非法的 files 条目：%r" % (source_file, entry))
            if not entry.get("reason"):
                raise ExclusionError(
                    "%s: collections.%s.files[%s] 缺少 reason"
                    % (source_file, collection, entry["path"]))
            target.files[str(entry["path"])] = FileRule(
                path=str(entry["path"]),
                reason=str(entry["reason"]),
                canonical=str(entry.get("canonical") or ""),
                added=str(entry.get("added") or ""),
                sha256=str(entry.get("sha256") or ""),
                source_file=source_file,
            )

    take_scope(data.get("defaults") or {}, glob, None)
    scopes: Dict[str, Scope] = {}
    for name, raw in (data.get("collections") or {}).items():
        scope = Scope()
        take_scope(raw or {}, scope, name)
        scopes[name] = scope
    return glob, scopes


def load(wiki_root: str) -> Exclusions:
    """读取并合并两份名单；文件不存在时返回空名单（不报错）。"""
    ex = Exclusions(wiki_root=wiki_root)
    for fname in (EXCLUDED_NAME, AUTO_NAME):
        glob, scopes = _parse(_read(os.path.join(wiki_root, fname)), fname)
        ex.global_scope.merge(glob)
        for name, scope in scopes.items():
            ex.scopes.setdefault(name, Scope()).merge(scope)
    return ex


def ensure_manual_file(wiki_root: str) -> str:
    """首次运行时生成可编辑的名单模板（不覆盖已有文件）。"""
    os.makedirs(wiki_root, exist_ok=True)
    path = os.path.join(wiki_root, EXCLUDED_NAME)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(HEADER)
    return path


def add_file_rule(wiki_root: str, collection: str, path: str, reason: str,
                  canonical: str = "", source_root: str = "") -> FileRule:
    """追加到 EXCLUSIONS.auto.yaml（不动人工维护的那份）。"""
    if not reason:
        raise ExclusionError("必须提供 reason")

    rule = FileRule(path=path, reason=reason, canonical=canonical,
                    added=datetime.date.today().isoformat())
    if source_root:
        abs_path = os.path.join(source_root, path)
        if os.path.exists(abs_path):
            rule.sha256 = sha256_file(abs_path)

    auto_path = os.path.join(wiki_root, AUTO_NAME)
    data = _read(auto_path) or {"version": 1, "defaults": {}, "collections": {}}
    scope = data.setdefault("collections", {}).setdefault(collection, {})
    entries = [e for e in (scope.get("files") or [])
               if not (isinstance(e, dict) and e.get("path") == path)]
    entries.append({k: v for k, v in {
        "path": rule.path, "reason": rule.reason, "canonical": rule.canonical,
        "added": rule.added, "sha256": rule.sha256,
    }.items() if v})
    scope["files"] = entries

    with open(auto_path, "w", encoding="utf-8") as f:
        f.write("# 由 `llm-wiki exclude add` 自动维护，请勿手工编辑\n")
        f.write("# 人工条目请写在 EXCLUSIONS.yaml\n\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return rule


def remove_file_rule(wiki_root: str, collection: str, path: str) -> bool:
    """从两份文件里都移除同 path 的条目。返回是否删掉了东西。"""
    changed = False
    for fname in (AUTO_NAME, EXCLUDED_NAME):
        full = os.path.join(wiki_root, fname)
        data = _read(full)
        if not data:
            continue
        scope = (data.get("collections") or {}).get(collection) or {}
        entries = scope.get("files") or []
        kept = [e for e in entries if not (isinstance(e, dict) and e.get("path") == path)]
        if len(kept) == len(entries):
            continue
        scope["files"] = kept
        changed = True
        with open(full, "w", encoding="utf-8") as f:
            if fname == AUTO_NAME:
                f.write("# 由 `llm-wiki exclude add` 自动维护，请勿手工编辑\n")
                f.write("# 人工条目请写在 EXCLUSIONS.yaml\n\n")
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return changed
