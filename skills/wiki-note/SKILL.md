---
name: wiki-note
description: |
  把高价值的回答或结论写回 wiki。先判断这份内容有原文依据（升级为正式页）
  还是纯判断（存为 note），再落盘、建索引、校验。
  笔记会被显式标记为「未经原文证实」，不会被当成事实引用。
tags: [wiki, note, writeback, 写回, 沉淀]
---

# Wiki 写回 (wiki-note)

## 项目路径配置

```bash
source "$HOME/.claude/skills/wiki-note/.env"
cd "$WIKI_DIR"
```

```bash
# .env
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
```

## 工作流程

### Step 1: 先判断值不值得写

三条判据，满足任意一条才写：

- 这个问题**以后大概率会被再问一次**
- 这个结论**改变了某个决策**（选型、边界、优先级）
- 这个归纳**跨了多篇文档**，下次重新推一遍成本很高

只满足「说得挺全」不写。wiki 会因为低价值页而贬值，不是因为页太少。

### Step 2: 再判断写成什么

| 内容性质 | 写成 | 判据 |
|---|---|---|
| 每个结论都能指回原文某节 | `topic` 页 | 它能自证 |
| 结论来自推理、权衡、讨论，原文没有直接对应 | `note` | **不要**硬挂引用 |
| 只是对已有页的补充 | 直接改那一页 | 不制造第二处事实 |

一句话：**能引用的写成正式页，不能引用的写成笔记。**

「硬挂一个相关引用让笔记看起来有据」是最坏的做法——`lint` 会拦住挂空引用的，
但拦不住引用错位的，那要靠你自己判断。

### Step 3: 写成笔记

```bash
"$WIKI_PYTHON" -m wikilib.cli note \
  --title "为什么把凭证代理做成独立层" \
  --slug why-broker-as-layer \
  --origin conversation \
  --link "凭证代理模式,令牌库"
```

参数：

| 参数 | 说明 |
|---|---|
| `--title` | 用**结论本身**当标题，不要用「关于 X 的思考」 |
| `--slug` | 省略时按标题生成；中文标题会退化成 `note-<时间戳>` |
| `--origin` | `conversation`（对话得出）/ `human`（人工判断）/ `imported`（外部导入） |
| `--link` | 关联页标题，逗号分隔；**至少链一个**，否则会成为孤儿页 |
| `--body FILE` | 直接导入一段 Markdown（比如 kg-qa 的回答） |

已有现成回答时，先存成文件再导入：

```bash
"$WIKI_PYTHON" -m wikilib.cli note --title "..." --body /tmp/answer.md --origin conversation
```

### Step 4: 写正文

脚手架会给三段骨架，按它填：

```markdown
# 标题

> **一句话**：把结论压缩成一句，别写「本文讨论了……」

## 背景
这个问题为什么会出现，当时的约束是什么

## 结论
判断本身 + 它成立的前提 + 什么情况下会失效

## 关联
- [[已有的页]]
```

要求：

- **写清适用条件**。笔记最容易变成「在某个场景下成立，却被当成普适结论」
- **不确定就写不确定**，标出哪些是有把握的、哪些是待验证的
- 全中文，术语沿用 `GLOSSARY.md`

### Step 5: 收尾

```bash
"$WIKI_PYTHON" -m wikilib.cli build
"$WIKI_PYTHON" -m wikilib.cli lint --strict
"$WIKI_PYTHON" -m wikilib.cli html
```

`lint` 对笔记的检查与正式页不同：

- 不要求七段结构，但至少一个二级标题
- 不要求逐节引用
- `origin` 必须是三个合法值之一
- **声明了 `sources` 就必须真的引用**，否则报 `note_declared_no_cite`

### Step 6: 告诉用户这条笔记的性质

回复时明确说：这是一条**未经原文证实**的判断，检索时会带 `uncorroborated: true`。
不要让它听起来像 wiki 里已有的结论。

## 注意事项

- **笔记不会被 `update` 覆盖**，但会参与检索与站点导航
- 笔记升级为正式页：复制到 `topics/`/`concepts/`，改 `type`、补引用、删 `origin`。
  直接改 `type` 会立刻被结构校验拦下——这是设计如此，不是 bug
- 不要把一次性的闲聊写进来；wiki 不是聊天记录归档
- 同一主题已经有笔记时，**改那一条**，不要新建第二条
