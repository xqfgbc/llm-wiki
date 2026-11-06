---
name: wiki-qa
description: |
  从 LLM Wiki 查询相关内容交给大模型。返回命中的中文概念/实体/主题页、
  它们引用的原始文档与小节，以及是否弱相关。
  与知识库问答（kg-qa）互补：wiki 给归纳与框架，原文给证据。
tags: [wiki, qa, retrieval, 检索]
---

# Wiki 问答 (wiki-qa)

## 项目路径配置

```bash
source "$HOME/.claude/skills/wiki-qa/.env"
cd "$WIKI_DIR"
```

```bash
# .env
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
```

## 工作流程

### Step 1: 检索

```bash
"$WIKI_PYTHON" -m wikilib.cli query "<用户问题>" --json
```

返回：

```json
{
  "query": "...",
  "shape": "factoid | synthesis",
  "expanded_terms": ["...", "credential"],
  "wiki_pages": [
    {"page": "wiki/.../credential-brokering.md", "title": "凭证代理模式",
     "type": "concept", "status": "active", "weak": false, "score": 43.4,
     "summary": "...", "body": "...", "matched_sections": ["..."],
     "cites": [{"doc": "...", "heading": "...", "orig_heading": "...",
                "origin_path": "Agent安全/....md"}]}
  ],
  "source_hits": [{"doc": "...", "heading": "...", "body": "..."}]
}
```

空结果是正常返回（退出码 0），不是故障。

### Step 2: 选择材料

按顺序判断：

1. **`uncorroborated: true` 的是笔记**：它承载原文里没有的判断。可以用它帮忙组织思路，
   但**不能当作事实依据**，引用时必须明说「这是笔记里的判断，未见原文支撑」
2. **`weak: true` 的页只作背景**，不要用它支撑结论——它表示标题/小节与问题没有直接词面对应
3. **`status: "stale"` 的页不能作依据**，可提示用户该页需要重编
4. **`status: "disputed"` 的页必须呈现双方说法**
5. `shape == "synthesis"`（对比、综述、有哪些、怎么落地）优先用 `wiki_pages` 定框架；
   `shape == "factoid"`（具体定义、某个数）优先用 `source_hits`

### Step 3: 回答

- **wiki 是组织层，不是事实层。** 事实性断言要么来自 `source_hits`，要么来自 wiki 页 `cites` 指向的原文
- 引用一个 wiki 页时，**必须同时给出它引用的原始文档与小节**：
  ```
  【wiki：凭证代理模式 → 原始来源：[[…Part 1#Credential Brokering 4 AI Agents (CB4A)]]】
  ```
- 不要用 Markdown 链接指向 wiki 页的 Obsidian 路径；用页面标题即可，用户能通过站点或 Obsidian 找到
- wiki 有、原文没有支撑时，明说「这是 wiki 的归纳，原文中没有直接对应表述」
- 完全没命中时，直接说没有，并说明 wiki 覆盖的是哪些目录（问一句 `collection list`）

### Step 4（可选）: 与知识库合并

如果同一问题也跑了 `kg-qa`，两边结果要**分开呈现**：

| | 知识库（原文） | Wiki |
|---|---|---|
| 角色 | 事实层，可直接引用 | 组织层，提供框架与关联 |
| 冲突时 | **以原文为准** | 提示该 wiki 页需重编 |

## 注意事项

- 中文提问会自动扩展出英文术语（`expanded_terms` 可见），因为语料多为英文；这不是改写问题，是跨语言召回
- 页面的 `body` 是完整正文，可以直接读；`matched_sections` 只标出命中位置
- 不要为了「答得像样」而引用 `weak` 页；检索不到就说检索不到
