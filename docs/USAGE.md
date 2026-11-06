# 使用说明

> 面向日常使用。安装与设计原理见 `README.md`，编译规则见 vault 里的 `wiki/CLAUDE.md`。

## 0. 三条要记住的事

1. **源目录只读。** 所有修复都发生在 `data/raw/` 的快照里，你的 Obsidian 原文一个字节都不会被改。
2. **wiki 是派生物。** 删掉 `data/` 和 `obsidian/wiki/<collection>/` 随时可以重建；真正的事实来源是源目录。
3. **人看 HTML，LLM 读 Markdown，kg-qa 走 query。** 三者是同一份内容的三种出口。

```bash
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
cd "$WIKI_DIR"
```

---

## 1. 日常：源文件改了怎么办

```
改了源 md
   ↓  update      比对 hash，算出哪些页受影响，产出 work order
   ↓  改页        按 work order 重写（人工或让 Claude 按 order 写）
   ↓  build       重新建索引（源切片 + wiki 页）
   ↓  lint        确定性校验，错误必须清零
   ↓  html        增量生成站点
```

```bash
"$WIKI_PYTHON" -m wikilib.cli update
#   → 差异：新增 0 · 变更 1 · 删除 0 · 未变 7
#   → work order 1 份 → data/orders/agent-security/defense-in-depth.json

"$WIKI_PYTHON" -m wikilib.cli build
"$WIKI_PYTHON" -m wikilib.cli lint --strict
"$WIKI_PYTHON" -m wikilib.cli html
```

**只改 `update` 列出来的页。** 没出现在列表里的页不应被改动——它们的字节必须保持不变，这是增量正确性的判据。

### work order 里有什么

```json
{
  "slug": "defense-in-depth",
  "mode": "recompile",
  "reasons": ["章节变更：pattern2:leastpermissions"],
  "existing_page": "（当前页面全文）",
  "sources": [{"doc_id": "...", "rel_path": "...", "headings": [...]}],
  "glossary": "（术语表片段）",
  "contract": { "sections": [...], "citation_format": "...", "rules": [...] }
}
```

`mode` 为 `new` 时表示这是候选新页，没有既有内容。

---

## 2. 新增文档

直接把 `.md` 丢进已登记的源目录，然后跑 `update`。系统会做 **triage**：

| 判定 | 条件 | 结果 |
|---|---|---|
| Update | 文档命中已有页的标题/别名 | 并入该页的 sources，标记重编 |
| New | 未命中，但内容足够 | 进入候选池，产出 `mode: new` 的 order |
| No material | 素材太薄 | 只记日志，不建页 |

`No material` 是**独占**的——不会一边说「不成材」一边又建出一个空洞的页。

---

## 2.5 技能（Claude Code 里用）

| 技能 | 用途 |
|---|---|
| `/wiki-update` | 增量更新。无参数 = 同步全部已登记目录；带路径 = 先校验登记再更新 |
| `/wiki-qa` | 从 wiki 查询内容交给大模型，附原始出处 |
| `/llm-wiki` | 底层命令入口（build / lint / html / exclude / usage 等） |

三个技能都是软链到 `~/.claude/skills/`，源在本仓库 `skills/`，改动即时生效。

### 源目录的硬约束

`collection add`（以及 `/wiki-update <路径>`）**只接受 Obsidian vault 内的子目录**：

| 拒绝 | 原因 |
|---|---|
| 不在 vault 内 | 事实源会散落到任意位置 |
| 等于 vault 根 | 会把 `wiki/` 自己也当成源 |
| 与 wiki 根重叠 | 派生物不能反过来当事实来源 |
| 不是目录 | 传了文件 |

没有绕过开关。若确实要换 vault，改 `config.local.yaml` 的 `obsidian.vault_root`，
而不是放宽校验。

## 3. 新增一个目录（collection）

```bash
"$WIKI_PYTHON" -m wikilib.cli collection add cloud-notes \
  --source /path/to/obsidian/笔记/云架构 --label "云架构笔记"

"$WIKI_PYTHON" -m wikilib.cli init                             # 建好 concepts/entities/topics/notes
"$WIKI_PYTHON" -m wikilib.cli build  --collection cloud-notes  # 第一次：只建索引
"$WIKI_PYTHON" -m wikilib.cli update --collection cloud-notes  # 建立基线
```

```bash
"$WIKI_PYTHON" -m wikilib.cli collection list      # 看现有 collection 与索引计数
"$WIKI_PYTHON" -m wikilib.cli collection remove X  # 只从配置移除，已生成的页面不会删
```

`collection add` 会校验源目录存在、拒绝重名；配置写回 `config.local.yaml`（该文件若含注释会被重写）。

多个 collection 共用同一套 `CLAUDE.md`、`GLOSSARY.md`、`EXCLUSIONS.yaml`，
`index.md` 里按 collection 分块，互不覆盖。

> 首次 `update` 只建立基线、不产出 order——否则已存在的文档会全部被当成「新增」。

---

## 4. 排除名单

两个文件，**运行时合并，互不覆盖**：

| 文件 | 谁写 | 用途 |
|---|---|---|
| `obsidian/wiki/EXCLUSIONS.yaml` | 你手写（Obsidian 里直接改） | 长期规则、带注释 |
| `obsidian/wiki/EXCLUSIONS.auto.yaml` | CLI 写入 | `exclude add` 的产物 |

四级粒度，**每条必须有 `reason`**（缺了会直接报错）：

```yaml
version: 1
defaults:
  headings: ["目录", "Contents", "Further Reading"]   # 对所有 collection 生效
  globs: ["**/Pasted image*"]
  terms: ["致谢"]
collections:
  agent-security:
    files:
      - path: "某机翻件.md"
        reason: "与英文原文重复"
        canonical: "原始英文.md"      # 可选：说明它对应哪份
```

```bash
"$WIKI_PYTHON" -m wikilib.cli exclude list
"$WIKI_PYTHON" -m wikilib.cli exclude add "某文件.md" --reason "重复件"
"$WIKI_PYTHON" -m wikilib.cli exclude why "某文件.md"     # 为什么这份没进 wiki
"$WIKI_PYTHON" -m wikilib.cli exclude remove "某文件.md"
```

**自检**：`status` 会报告两类问题——被排除的文件后来**变了**（提示你复核是否仍需排除），
以及 `canonical` 指向的文件**已不存在**（名单过期）。

---

## 5. 审阅与质检

```bash
"$WIKI_PYTHON" -m wikilib.cli lint            # 全部输出
"$WIKI_PYTHON" -m wikilib.cli lint --strict   # 有 error 时退出码非 0
```

### 硬错误（必须修）

| 规则 | 含义 |
|---|---|
| `cite_unresolved` | 引用指向的文档或小节不存在 |
| `section_no_citation` | 某节没有任何引用（关系段与来源索引豁免） |
| `structure` / `h1_mismatch` | 7 段结构或 H1 不符 |
| `cjk_ratio` | 中文占比低于 0.45 |
| `banned_term` | 命中 `GLOSSARY.md` 的禁用译法 |
| `page_too_long` | 正文超过 3000 字（引用标记不计） |
| `undeclared_citation` | 正文引用了、`sources` 里没声明 |
| `duplicate_slug` / `duplicate_name` | slug 或标题/别名冲突 |
| `meta_missing` / `type_invalid` / `status_invalid` | frontmatter 不合规 |

### 警告（可暂留）

`orphan`（没被别的页链接）、`red_link`（指向尚未建页的概念）、
`unused_source`（声明了但没引用）、`stale`（来源内容变了）、`unstamped`（未记录内容 hash）。

> **`red_link` 不是错误，是知识缺口。** 它告诉你哪些概念被提到了但还没建页。

### lint 查不到的东西

`lint` 只能判断「引用指向的小节存在」，判断不了「这一节的内容真的支持这句断言」。
语义错配必须人工抽查，优先看：实体页里的**数字与机制描述**、`争议` 段里的**观点归属**。

### `stamp` 的作用

```bash
"$WIKI_PYTHON" -m wikilib.cli stamp
```

编译完一页后，把当前各小节的内容 hash 写进该页 frontmatter 的 `cites[]`。
没有这个 hash，系统只知道「小节还在」，不知道「内容变没变」。
源文件改动后 hash 不匹配 → `lint` 报 `stale`；把源文件还原，`stale` 会自动消失。

---

## 6. Token 用量观测

```bash
"$WIKI_PYTHON" -m wikilib.cli usage          # 累计 + 最近 12 次
"$WIKI_PYTHON" -m wikilib.cli usage --json   # 机读
```

站点首页底部有同名的 **Token 用量** 面板，顶部状态栏也会显示累计值。

### 两种数字，不要混着看

| | 来源 | 含义 | 何时有 |
|---|---|---|---|
| **估算** | 管道自己算 | 交给模型的文本量 × 固定系数（中文 0.9/字，其余 0.25/字符） | 每次 `build` / `update` 自动记录 |
| **实测** | Claude Code 会话记录 | 真实的输入/输出 token，含推理与工具往返 | 手动 `usage --import-dir` 导入 |

估算**不含**模型推理开销与系统提示，因此总是低于实测。

### 每次操作记录了什么

- `build`：本次**改动了哪些页**、这些页引用了多少来源字数（输入）、产出多少正文字数（输出）
- `update`：本次产出多少份 work order、order 正文多少字（输入）

记录写在 `data/usage.jsonl`，一行一次，可自行追加或删除。

### 导入真实用量

```bash
"$WIKI_PYTHON" -m wikilib.cli usage                       # 不传参数：自动找最近写入的会话
"$WIKI_PYTHON" -m wikilib.cli usage --import-dir <目录>    # 指定项目目录
"$WIKI_PYTHON" -m wikilib.cli usage --session <id>        # 指定单个会话
```

两条约定：

- **按会话记增量**：会话记录里的数字是该会话的累计值，导入时只记
  「当前累计 − 该会话已导入过的量」。同一会话重复导入会提示「已是最新」，不会翻倍。
- **自动包含子代理**（`<session>/subagents/*.jsonl`）——并行编译的消耗占相当比例，
  只统计主会话会漏掉约三分之一。

**实测值不会自动累加**：换一个 session 做增量后，需要在那个 session 结束时再跑一次
`usage`。估算值不需要这一步——`build` / `update` 会自动记录。

---

## 6. 看结果

```bash
"$WIKI_PYTHON" -m wikilib.cli html --open     # 生成并打开
```

- 站点在 `data/site/`，**纯静态、可离线**，双击 `index.html` 即可
- 引用链接是 `obsidian://`，点了会跳到 Obsidian 原文的对应标题
- 页内搜索走内联索引，不需要后端；**窄屏时左侧栏隐藏，搜索栏会移到页面顶部**
- 支持多词查询（`KMS grant`）、↑↓ 选择、回车打开、Esc 关闭
- 增量生成：内容没变的页会跳过（第二次运行通常 0.2 秒内）

在 Obsidian 里也可以直接看：`obsidian/wiki/<collection>/{concepts,entities,topics}/`。
Obsidian 的双链、反链、图谱都能直接用。

---

## 8. 给 kg-qa 用

```bash
"$WIKI_PYTHON" -m wikilib.cli query "Agent 调用 API 时凭证放在哪里" --json
```

输出：

```json
{
  "query": "...",
  "shape": "factoid | synthesis",
  "expanded_terms": ["...", "credential"],
  "wiki_pages": [
    {"page": "...", "title": "凭证代理模式", "type": "concept", "status": "active",
     "weak": false, "score": 43.4, "summary": "...", "body": "...",
     "cites": [{"doc": "...", "heading": "...", "orig_heading": "...", "origin_path": "..."}]}
  ],
  "source_hits": [...]
}
```

几点约定：

- **失败时返回空列表、退出码 0** —— wiki 没建好不能把 kg-qa 拖垮
- `weak: true` 表示该页与问题没有直接词面对应，只能当背景参考
- `shape` 决定先读哪边：`synthesis` 先读 wiki 定框架，`factoid` 先读原文
- 中文提问会自动扩展出英文术语（语料多为英文，不扩展就召不回来）

`kg-qa` 会分别调用原文知识库与 wiki 两条命令，在模型上下文里合并。
两边的分工写在 `kg-tools/kg-qa/SKILL.md` 的 Step 3.1。

---

## 9. 什么放 wiki、什么放知识库

| | 知识库（AI-KG） | Wiki（llm-wiki） |
|---|---|---|
| 输入 | PPT / 报告 → 人工转换 → Obsidian | 网页剪藏、笔记、任意指定目录 |
| 产物 | 带页码的原文证据 | 跨文档归纳的中文概念页 |
| 强项 | 定位到「原文第 N 页」 | 回答「这几个概念什么关系」 |
| 更新 | 新增文档 → 重建索引 | 源变更 → 只重编受影响页 |

**不要**把 PPT 派生的文档再放进 wiki 目录——它们已经通过知识库被检索到，重复入库只会制造两套互相矛盾的证据。

---

## 10. 常见问题

**为什么我的标题被改了？**
网页剪藏常把标题和正文首句粘在一起（`### 新的攻击面STS 将所有…`）。规范化只做「断行」，不改字，且只改快照不改源文件。想看改了什么：`normalize --dry-run`。

**为什么引用链接那么长？**
锚点用的是**原始标题**（含粘连部分），因为 Obsidian 只认源文件里真实存在的文本。界面上显示的是短标签，完整名称在鼠标悬停提示里。

**为什么某页显示「陈旧」？**
它引用的小节内容变了。跑 `update` 看差异，重编该页，再 `stamp`。

**中文占比不达标怎么办？**
通常是把英文原文整段照抄了。引用标记不计入占比，所以不会因为引用多而失分。

**`data/` 能删吗？**
能。删掉后跑 `build` + `html` 重建。但 `wiki_state.sqlite` 删掉后，下一次 `update` 会把所有源当成「新增」重新建基线——不会出错，只是白跑一轮。

**换电脑怎么迁移？**
仓库只含代码。新机器上：`uv venv` → 装依赖 → 复制一份 `config.local.yaml` →
`init`（重新生成 `CLAUDE.md` / `GLOSSARY.md` / `EXCLUSIONS.yaml` 模板）→ `build`。
wiki 页在 Obsidian 里，跟着 vault 走。

**能不能让 wiki 引用 PPT 文档？**
技术上可以（把 `obsidian/AI` 加成 collection），但目前是刻意不做的：会与知识库重复，且 PPT 派生的文档已经足够结构化，再归纳一遍收益低。

---

## 11. 命令速查

| 命令 | 作用 | 耗时 |
|---|---|---|
| `normalize --dry-run` | 看规范化会改什么，不落盘 | 秒级 |
| `normalize` | 写快照到 `data/raw/` | 秒级 |
| `build` | 建 `data/wiki.sqlite`（源切片 + wiki 页） | 秒级 |
| `update` | 算增量、产出 work order | 秒级 |
| `stamp` | 把内容 hash 写进页面 | 秒级 |
| `lint [--strict]` | 确定性校验 | 秒级 |
| `html [--open]` | 增量生成站点 | 0.3 秒量级 |
| `query "<问题>" --json` | 检索（给 kg-qa 用） | 毫秒级 |
| `usage [--json] [--import-dir]` | Token 用量统计与导入 | 秒级 |
| `collection list/add/remove` | 目录（collection）管理 | 秒级 |
| `init` | 部署 schema / 术语表 / 排除名单模板 | 秒级 |
| `status` | 概览 + 排除名单自检 | 秒级 |
| `exclude list/add/why/remove` | 排除名单维护 | 秒级 |

所有命令都支持 `--collection <name>`；只有一个 collection 时可省略。
