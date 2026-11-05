"""配置加载：config.yaml（默认值，入库）+ config.local.yaml（本机，不入库）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "config.yaml")
LOCAL_CONFIG = os.path.join(REPO_ROOT, "config.local.yaml")

DEFAULTS: Dict[str, Any] = {
    "max_chunk_chars": 2500,
    "hard_chunk_chars": 5000,
    "max_heading_level": 4,
    "max_page_chars": 3000,
    "min_sources_per_page": 2,
    "max_sources_per_page": 15,
    "language": "zh",
    "min_cjk_ratio": 0.45,
    "heading_repair_max_len": 40,
    "heading_repair_min_spill": 20,
    "warn_delete_ratio": 0.4,
    "wiki_top_k": 3,
}

EXIT_CONFIG = 3


class ConfigError(RuntimeError):
    pass


@dataclass
class ObsidianConfig:
    vault_root: str
    vault_name: str
    wiki_root: str


@dataclass
class CollectionConfig:
    name: str
    label: str
    sources: List[str]
    exclude_headings: List[str] = field(default_factory=list)

    @property
    def slug_dir(self) -> str:
        return self.name


@dataclass
class WikiConfig:
    obsidian: ObsidianConfig
    data_dir: str
    defaults: Dict[str, Any]
    collections: List[CollectionConfig]
    repo_root: str = REPO_ROOT

    # ---- 派生物路径 ----
    @property
    def raw_dir(self) -> str:
        return os.path.join(self.data_dir, "raw")

    @property
    def index_db(self) -> str:
        return os.path.join(self.data_dir, "wiki.sqlite")

    @property
    def state_db(self) -> str:
        return os.path.join(self.data_dir, "wiki_state.sqlite")

    @property
    def site_dir(self) -> str:
        return os.path.join(self.data_dir, "site")

    @property
    def orders_dir(self) -> str:
        return os.path.join(self.data_dir, "orders")

    def collection(self, name: Optional[str] = None) -> CollectionConfig:
        if not self.collections:
            raise ConfigError("config.local.yaml 里没有配置任何 collection")
        if name is None:
            if len(self.collections) > 1:
                raise ConfigError(
                    "存在多个 collection，请用 --collection 指定：%s"
                    % ", ".join(c.name for c in self.collections))
            return self.collections[0]
        for c in self.collections:
            if c.name == name:
                return c
        raise ConfigError("未找到 collection: %s（可用：%s）"
                          % (name, ", ".join(c.name for c in self.collections)))

    def get(self, key: str) -> Any:
        return self.defaults.get(key, DEFAULTS.get(key))


def _load_yaml(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve(path: str) -> str:
    if not path:
        return path
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(REPO_ROOT, path))


def load(local_path: Optional[str] = None) -> WikiConfig:
    base = _load_yaml(DEFAULT_CONFIG)
    local = _load_yaml(local_path or LOCAL_CONFIG)
    if not local:
        raise ConfigError(
            "缺少 config.local.yaml。请先执行：\n"
            "  cp config.local.yaml.example config.local.yaml\n"
            "然后填写 obsidian.vault_root / vault_name / wiki_root 与 collections。")

    defaults = dict(DEFAULTS)
    defaults.update(base.get("defaults") or {})

    obs = local.get("obsidian") or {}
    for key in ("vault_root", "vault_name", "wiki_root"):
        if not obs.get(key):
            raise ConfigError("config.local.yaml 缺少 obsidian.%s" % key)

    obsidian = ObsidianConfig(
        vault_root=_resolve(obs["vault_root"]),
        vault_name=str(obs["vault_name"]),
        wiki_root=_resolve(obs["wiki_root"]),
    )

    collections = []
    for raw in local.get("collections") or []:
        if not raw.get("name"):
            raise ConfigError("collection 缺少 name 字段")
        sources = [_resolve(s) for s in (raw.get("sources") or [])]
        if not sources:
            raise ConfigError("collection %s 没有 sources" % raw["name"])
        for s in sources:
            if not os.path.isdir(s):
                raise ConfigError("collection %s 的源目录不存在：%s" % (raw["name"], s))
        collections.append(CollectionConfig(
            name=str(raw["name"]),
            label=str(raw.get("label") or raw["name"]),
            sources=sources,
            exclude_headings=[str(h) for h in (raw.get("exclude_headings") or [])],
        ))

    data_dir = _resolve(str(local.get("data_dir") or "data"))

    return WikiConfig(obsidian=obsidian, data_dir=data_dir,
                      defaults=defaults, collections=collections)
