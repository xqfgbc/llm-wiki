---
name: wiki-extract
description: |
  LLM Wiki 的实体抽取与对抗复核（P1）。从源文档生成候选、做确定性门禁，
  再由 proposer / challenger / arbiter 三个隔离上下文完成对抗复核；
  只有 AI 无法收敛的候选进入人审队列。
tags: [wiki, extraction, entity, adversarial, registry]
---

# Wiki 实体抽取与对抗复核 (`wiki-extract`)

## 路径

```bash
source "$HOME/.claude/skills/wiki-extract/.env"
cd "$WIKI_DIR"
```

> 必须用 `$WIKI_PYTHON`（独立 venv，缺 jieba 的系统 python 不能用）。

## 核心原则

1. **三个上下文必须隔离**：Proposer、Challenger、Arbiter 不能共享对话。
   Challenger 只读盲审工单，**不得读 `proposals.json`**。
2. **AI 能定就不交人**。只有 `review apply` 写入 `_human_queue.md` 的才交人。
3. **每个结论都要能追到证据**；没有证据的反对无效。
4. **词表是唯一命名权威**。新缩写先登记，再建页。

## 工作流程

### Step 1：候选 + 门禁

```bash
"$WIKI_PYTHON" -m wikilib.cli extract --collection <name>
# 增量：
"$WIKI_PYTHON" -m wikilib.cli extract --collection <name> --changed-only
```

产出：

```
data/orders/<collection>/extract/_candidates.json   全部候选 + 证据 + 门禁
data/orders/<collection>/extract/_propose.json      proposer 工单
data/review/<collection>/_pending.json              预复核清单
```

先看摘要。`已覆盖` 不用管；`观察池` 是低信号，不送复核；`待复核` 才进入下一步。

### Step 2：Proposer（独立上下文 A）

读 `_propose.json`，逐条产出提案，写到：

```
data/review/<collection>/proposals.json
```

输出必须是 JSON 数组：

```json
[
  {
    "candidate_id": "natgateway:8ca0f1",
    "action": "accept_new",
    "canonical": "NAT Gateway",
    "type": "entity",
    "aliases": ["NAT 网关"],
    "merge_into": null,
    "qualifier": null,
    "confidence": 0.86,
    "evidence_used": ["aws-k8s-gitops...#3.1 VPC——云上的私有网络"]
  }
]
```

规则：

- `action` 只能是 `accept_new | merge_into | split | alias_only | reject | defer`
- 缩写先展开成全称，全称进 aliases；canonical 不能用缩写
- 同名不同义用 `qualifier` 区分
- 上位概念不能与下位对象合并（Istio ≠ Istio Ambient）
- 证据不足就 `defer`
- 不得引入工单证据之外的事实

### Step 3：生成盲审工单

```bash
"$WIKI_PYTHON" -m wikilib.cli review build --collection <name>
# → data/review/<collection>/_challenge.json
```

这份工单已经剥掉了 proposer 的置信度、理由和 proposed_by。

### Step 4：Challenger（独立上下文 B，盲审）

**只读 `_challenge.json`，不要读 proposals.json。**

逐条按 12 项攻击清单检查：

| # | 攻击点 | 判什么 |
|---|---|---|
| A | 证据充分性 | 有定义句，还是只被提及；是否只在代码/表格 |
| B | 同一性 | 是否只是另一个候选的别名/缩写/中英写法 |
| C | 粒度 | 是否是现有页的部件/模式/实例（应 merge_into） |
| D | 类型 | entity / concept / topic 是否判对 |
| E | 边界 | 定义与边界能否写清；是否过于泛化 |
| F | 同形异义 | 同名是否指不同东西；是否要 qualifier |
| G | 别名安全 | alias 是否是上位词，会把查询带偏 |
| H | 重复 | 是否与现有页近重复 |
| I | 稳定性 | 是否版本号/一次性项目名/代码变量名 |
| J | 来源质量 | 是否来自机翻件或重复件 |
| K | 可解析 | doc+heading 是否真实存在 |
| L | 矛盾 | 是否与现有页定义/数字/结论冲突 |

输出到 `data/review/<collection>/challenges.json`：

```json
[
  {
    "candidate_id": "natgateway:8ca0f1",
    "verdict": "revise",
    "objections": [
      {
        "rule": "C",
        "severity": "minor",
        "evidence": "3.1 节把 NAT Gateway 描述为 VPC 的出口组件",
        "fix": "并入 vpc 页的『关键构成』"
      }
    ],
    "revised_proposal": {
      "action": "merge_into",
      "canonical": "VPC",
      "type": "service",
      "aliases": ["NAT 网关"],
      "merge_into": "vpc",
      "qualifier": null,
      "confidence": 0.88
    }
  }
]
```

规则：

- `verdict` 只能是 `uphold | revise | reject | defer`
- `blocker` 必须给出可执行替代方案（merge_into / qualifier / reject 理由），否则无效
- 不确定就 `defer`，不要为了严格而一概反对
- 每个反对都要引用证据；空泛反对无效

### Step 5：Arbiter（仅分歧）

只处理 `verdict` 与 proposer 不一致、且双方都有证据的条目。写到：

```
data/review/<collection>/arbiters.json
```

没有分歧就不需要这一步。

### Step 6：合并裁决

```bash
"$WIKI_PYTHON" -m wikilib.cli review apply --collection <name>
```

产出：

```
obsidian/wiki/entities.yaml                 注册表（人可编辑）
data/orders/<collection>/<slug>.json        新页/合并页工单
data/review/<collection>/_resolved.json     每个候选的最终裁决
data/review/<collection>/_human_queue.md    只交给人/AI 下一轮的清单
```

看摘要：`人审 N` 是唯一需要人的数量。目标 <10%。

### Step 7：写页

按 `data/orders/<collection>/*.json` 的 `mode: new` 工单写页，遵守
`obsidian/wiki/CLAUDE.md` 的七段结构与引用契约。

**先读 order 的 `evidence[]`**：每条含 `doc / heading / quote / definitional`。
事实性断言只从这些 quote 里取；需要更多上下文时只读 `doc#heading`，不要通读整篇源文档。
证据不足就在「适用范围与局限」里说明，不要补写。

已有的 order 若没有证据包，先跑：

```bash
"$WIKI_PYTHON" -m wikilib.cli review evidence
```

写完后：

```bash
"$WIKI_PYTHON" -m wikilib.cli build
"$WIKI_PYTHON" -m wikilib.cli lint --strict
"$WIKI_PYTHON" -m wikilib.cli html
```

### Step 8：跨 collection 与出处

词表/注册表是全局的，不按 collection 切开。需要看一个词的全部出处：

```bash
"$WIKI_PYTHON" -m wikilib.cli lexicon provenance VPC
# → agent-security / tech-architecture 各自的 doc#heading 与引文
```

需要跨 collection 统一抽取（一个候选池）：

```bash
"$WIKI_PYTHON" -m wikilib.cli extract --global
"$WIKI_PYTHON" -m wikilib.cli review build --global
"$WIKI_PYTHON" -m wikilib.cli review apply --global
```

逐 collection 增量仍然可用；每个候选的证据都记录来源 collection，
页面 order 落到证据最多的那个 collection。

### Step 9：补词表

复核中遇到的未登记缩写：

```bash
"$WIKI_PYTHON" -m wikilib.cli lexicon missing
"$WIKI_PYTHON" -m wikilib.cli lexicon add --kind acronym \
  --name CNI --expansion "Container Network Interface" --zh "容器网络接口"
"$WIKI_PYTHON" -m wikilib.cli lexicon check
```

## 人审工作台（推荐）

```bash
"$WIKI_PYTHON" -m wikilib.cli review html
# → data/review/human_review.html
```

用浏览器打开 **`data/review/human_review.html`**（不是 `templates/` 下的模板；
`review html --open` 会直接调起），按 collection / 原因 / 状态筛选，逐条：

1. 选择 `accept_new / merge_into / alias_only / reject / defer`；
2. 需要时补 canonical、type、aliases、merge_into、qualifier；
3. 写备注；
4. 勾“已复核”。

选择自动存在浏览器；点“生成决策 JSON”后复制或另存，再回灌：

```bash
"$WIKI_PYTHON" -m wikilib.cli review human-apply --file <导出的 JSON>
```

导出格式固定为 `docs/human_decisions.schema.json`。页面每次 `review html`
重新生成，未导入的条目会继续留在队列里。

## 人审队列怎么处理

也可以直接读 `_human_queue.md`，每条都有：

- `candidate_id`
- 提案（action/canonical/type/confidence）
- 触发原因
- 反对意见（rule/severity/evidence）
- 2 条证据
- 可选处理

处理方式：

1. 能定的，补一条最终决定到 `data/review/<collection>/human_decisions.json`（格式同 proposals+challenges 的并集，`reviewer: human`），重跑 `review apply --proposals human_decisions.json --challenges ...`；
2. 不能定的，在 `_human_queue.md` 里写明缺什么证据，保持 `defer`。

不要绕过队列直接改页面：注册表不更新，下一轮抽取还会重复问同一个问题。

## 验收

- `review apply` 的人审率 ≤ 10%
- 新增页中低价值/重复页 ≤ 5%
- `lint --strict` 0 错误
- `registry check` 0 冲突
- `lexicon check` 0 冲突、0 缺全称
