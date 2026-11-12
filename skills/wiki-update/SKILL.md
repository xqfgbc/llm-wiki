---
name: wiki-update
description: |
  增量更新 LLM Wiki。不带参数时，从已登记的全部 Obsidian 目录同步变更；
  带路径参数时，先把该目录校验并登记为新的 collection，再一并更新。
  只接受 Obsidian vault 内的子目录——事实源必须唯一。
tags: [wiki, update, obsidian, incremental, 增量]
---

# Wiki 增量更新 (wiki-update)

## 项目路径配置

```bash
source "$HOME/.claude/skills/wiki-update/.env"
cd "$WIKI_DIR"
```

```bash
# .env
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
```

> **⚠️ 必须用 `$WIKI_PYTHON`**，不要用裸 `python3`（缺 jieba）。

## 工作流程

### Step 1: 处理参数

**不带参数** —— 同步所有已登记目录：

```bash
"$WIKI_PYTHON" -m wikilib.cli collection list --json
# → [{"name":"agent-security","label":"Agent 安全","sources":[...],"docs":8,"pages":16}]
```

**带路径参数** —— 先登记再同步：

```bash
"$WIKI_PYTHON" -m wikilib.cli collection add <目录名> --source "<路径>"
```

路径会被强制校验，以下情况一律拒绝，**不要试图绕过**：

| 拒绝原因 | 说明 |
|---|---|
| 不在 vault 内 | 路径不在配置的 `obsidian.vault_root` 之下 |
| 不是目录 | 传了文件 |
| 等于 vault 根 | 必须指定 vault 下的具体子目录 |
| 与 wiki 根重叠 | 那是派生物，不能当事实来源 |

命令返回「该路径已登记为 collection『X』」时，说明无需重复添加，直接进入 Step 2。

### Step 2: 计算差异

对每个 collection 执行：

```bash
"$WIKI_PYTHON" -m wikilib.cli update --collection <name>
```

输出形如：

```
差异：新增 1 · 变更 2 · 删除 0 · 未变 7
  · defense-in-depth ← Defense in depth for autonomous AI agents（章节变更：…）
work order 3 份 → data/orders/agent-security
```

**没有变更就跳过 Step 3**，直接进入 Step 4 收尾。

### Step 3: 按 work order 重写页面

逐份读 `data/orders/<collection>/<slug>.json`，按 `obsidian/wiki/CLAUDE.md` 的契约重写整页：

1. 七段结构固定，顺序不得变更
2. 每条事实性断言带 `【[[文档标题#规范化小节标题]]】`
3. 全中文，术语用 `GLOSSARY.md` 的译法，专有名词保留英文
4. 不同来源冲突时并列，不擅自裁决
5. **只改 order 里列出的页**——没列出的页必须保持字节不变

order 的 `mode` 为 `new` 时是候选新页，没有既有内容。

### Step 4: 收尾

> 想精确统计这次编译消耗的 token，在 Step 3 之前先跑
> `"$WIKI_PYTHON" -m wikilib.cli usage --checkpoint`，
> 收尾后再 `usage --import --kind content`。开发 wiki 本身的消耗用 `--kind dev` 导入，
> 不要混进内容成本。



```bash
"$WIKI_PYTHON" -m wikilib.cli stamp            # 写入内容 hash，供陈旧检测
"$WIKI_PYTHON" -m wikilib.cli build            # 重建索引（源切片 + 页面）
"$WIKI_PYTHON" -m wikilib.cli lint --strict    # 必须 0 错误
"$WIKI_PYTHON" -m wikilib.cli html             # 增量生成站点
```

`lint` 有硬错误时必须修完再结束，不要「先跳过」。常见错误：

| 规则 | 处理 |
|---|---|
| `cite_unresolved` | 引用的文档或小节不存在，检查标题是否用了规范化后的写法 |
| `section_no_citation` | 给该节补引用（来源索引与关系段豁免） |
| `undeclared_citation` | 正文引用了但 `sources` 没声明 |
| `banned_term` | 命中禁用译法，查 `GLOSSARY.md` |
| `cjk_ratio` | 英文照抄太多，改写为中文 |

### Step 5: 报告

给用户一份简短交代：

- 处理了哪些 collection、新增/变更/删除各几篇
- 重写了哪几页、每页引用数
- `lint` 结果
- 站点路径

## 注意事项

- **源目录只读**：所有修补都发生在 `data/raw/` 快照里。
- **work order 是唯一依据**：不要自行扩大重编范围，否则增量就不可信了。
- `lint` 的 `red_link` 警告是知识缺口，不是错误；`stale` 说明来源变了但页面没重编。
- 首次对某个 collection 跑 `update` 只建立基线、不产出 order——这是正常的。
