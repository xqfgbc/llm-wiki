---
name: llm-wiki
description: |
  把指定的 Obsidian 目录编译成中文 LLM Wiki：概念/实体/主题页，带断言级引用，
  支持增量更新，产出 Markdown（供 LLM）与静态 HTML（供人）。
  与 AI-KG 知识库完全独立，只在 kg-qa 查询时被调用。
tags: [wiki, knowledge-base, obsidian, incremental, 知识编译]
---

# LLM Wiki 编译 (`llm-wiki`)

## 项目路径配置

```bash
cp .env.example .env    # 首次使用
```

```bash
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
```

> **⚠️ 必须用 `$WIKI_PYTHON`。** 这个 venv 与 AI-KG 的 `~/.venvs/ai-kg` 是分开的，
> 两边依赖互不影响。系统自带的 `/usr/bin/python3` 缺 jieba。

## 工作流程

### Step 0: 看状态

```bash
source "{skill_dir}/.env"
cd "$WIKI_DIR" && "$WIKI_PYTHON" -m wikilib.cli status
```

会列出每个 collection 的源文档数、wiki 页数，以及排除名单的自检结果
（被排除的文件是否变更、`canonical` 是否悬空）。

### Step 1: 首次编译

```bash
# 先 dry-run：看标题断行修复的对照，人工确认无误再落盘
"$WIKI_PYTHON" -m wikilib.cli normalize --dry-run

"$WIKI_PYTHON" -m wikilib.cli build      # 建索引（源切片 + 现有 wiki 页）
```

`normalize` 只把修好的快照写到 `data/raw/<collection>/`，**源目录一个字节都不改**。

### Step 2: 编译页面

`update` 会算出受影响页并产出 work order：

```bash
"$WIKI_PYTHON" -m wikilib.cli update
# → data/orders/<collection>/<slug>.json
```

每份 order 含：目标 slug、既有页面内容、来源切片、**证据包（evidence[]）**、
术语表片段、输出契约。有 `evidence[]` 时**先读它**，不要通读源文档；
每条断言只能来自 `evidence[].quote`。

按 `obsidian/wiki/CLAUDE.md` 的规则撰写，**逐条断言都要带引用**：

```
【[[文档标题#规范化小节标题]]】
```

写完后：

```bash
"$WIKI_PYTHON" -m wikilib.cli build          # 重新索引页面
"$WIKI_PYTHON" -m wikilib.cli lint --strict  # 硬错误必须清零
```

### Step 2.5: 新实体抽取与对抗复核

`update` 发现新文档没有命中任何已有页时，会写
`data/orders/<collection>/_unmatched.json`。这些文档**不要手工建页**，
交给抽取流程：

```bash
"$WIKI_PYTHON" -m wikilib.cli extract --collection <name>
# → data/orders/<name>/extract/_candidates.json / _propose.json

# Claude 按 _propose.json 产出 proposals.json
"$WIKI_PYTHON" -m wikilib.cli review build --collection <name>
# → _challenge.json（盲审工单）
# Claude 盲审后产出 challenges.json

"$WIKI_PYTHON" -m wikilib.cli review apply --collection <name>
# → entities.yaml + 新页 order + _human_queue.md

# 已有 order 缺证据包时：
"$WIKI_PYTHON" -m wikilib.cli review evidence
```

人审工作台（与 `html` 站点不同）：

```bash
"$WIKI_PYTHON" -m wikilib.cli review html --open
# 勾选/备注后导出 JSON：
"$WIKI_PYTHON" -m wikilib.cli review human-apply --file <导出的 JSON>
```

完整规则见 `docs/EXTRACTION.md` 与 `skills/wiki-extract/SKILL.md`。
跨 collection 统一抽取加 `--global`。

### Step 3: 生成站点

```bash
"$WIKI_PYTHON" -m wikilib.cli html --open
```

增量生成：内容没变的页会跳过。站点在 `data/site/`，可离线打开。

### Step 4: 日常增量

```bash
"$WIKI_PYTHON" -m wikilib.cli update      # 只看差异，产出 order
# → 按 order 重写受影响页
"$WIKI_PYTHON" -m wikilib.cli build && "$WIKI_PYTHON" -m wikilib.cli lint --strict
"$WIKI_PYTHON" -m wikilib.cli html
```

**变更范围由依赖图决定，不要自己另找**：`update` 输出的页就是全部要改的页。
未出现在列表里的页不应被改动——它们的字节必须保持不变。

### Step 5: 给 kg-qa 用

```bash
"$WIKI_PYTHON" -m wikilib.cli query "<问题>" --json
```

输出 `{query, shape, expanded_terms, wiki_pages[], source_hits[]}`。
失败时返回空 `wiki_pages` 且退出码为 0——wiki 没建好不得拖垮问答。

## 实体层命令

| 命令 | 作用 |
|---|---|
| `extract` | 候选抽取 + 确定性门禁，产出 proposer 工单 |
| `review build\|apply\|pending\|status` | 对抗复核与合并裁决 |
| `review html` | 生成可勾选/备注的人审工作台 `data/review/human_review.html` |
| `review human-apply` | 回灌人审工作台导出的统一 JSON |
| `review evidence` | 给已有 `mode:new` order 回填 evidence packet |
| `review prune` | 清理已完成/重复的 `mode:new` order 到 `_stale/` |
| `registry list\|show\|check` | 全局实体注册表 `entities.yaml` |
| `lexicon list\|add\|check\|missing\|provenance` | 全局词汇表与跨 collection 出处 |

## 排除名单

`obsidian/wiki/EXCLUSIONS.yaml` 可手工编辑（Obsidian 里直接改），
`EXCLUSIONS.auto.yaml` 由 CLI 写入，运行时合并，互不覆盖。

```bash
"$WIKI_PYTHON" -m wikilib.cli exclude add "某机翻件.md" --reason "与英文原文重复"
"$WIKI_PYTHON" -m wikilib.cli exclude why "某机翻件.md"
"$WIKI_PYTHON" -m wikilib.cli exclude list
```

四级粒度：`files` / `globs` / `headings` / `terms`，**每条必须有 reason**。

## 注意事项

- **源目录只读**。所有修补都发生在 `data/raw/` 的快照里。
- **改切分参数或修复规则会让快照 hash 变化**，随后 `update` 会报大面积变更——这是正常的，重编一次即可。
- **中文占比不达标**通常是把英文原文整段照抄了，要译文；引用标记不计入占比。
- `lint` 的警告里，`red_link` 是**知识缺口**（有概念还没建页），不是错误。
- 数据不出本机：`data/` 与 `config.local.yaml` 都在 `.gitignore`，提交前跑
  `git grep -nE '/(Users|home)/'` 自检。
