# 实体抽取与对抗复核（P1）

> 这份文档描述 `extract` / `review` / `registry` / `lexicon` 四个命令背后的规则。
> 面向维护者；日常使用见 `skills/wiki-extract/SKILL.md`。

## 为什么需要它

旧流程只在 collection 首次编译时让 LLM 按**小节标题**划分一次页面，之后：
增量 triage 只做文件名/标题的子串匹配；未命中的新文档进 `(候选)` 后被直接丢弃。
结果：只在正文出现的实体（NAT Gateway、VPC、ALB）永远没有页面；
上位词又被窄页吸收（Istio 挂在 istio-ambient 上）。

P1 把「抽取」拆成确定性候选 + 对抗复核两段，并把结果沉淀为注册表与词汇表。

## 流程

```
S1 候选生成        extract.py   正文/标题/词表种子 → 候选 + 证据
S2 确定性门禁      extract.py   覆盖/频次/缩写/同名/上下位 → covered|observation|needs_review
S3 Proposer        LLM          按 _propose.json 产出 proposals.json
S4 Challenger      LLM（盲审）  按 _challenge.json + 12 项攻击清单产出 challenges.json
S5 Arbiter         LLM（可选）  只处理 proposer 与 challenger 的分歧
S6 注册表合并      review.py    写 entities.yaml、建页 order、人审队列
S7 写页            LLM          按 order 写七段页面
S8 词表维护        lexicon.py   未登记缩写 → GLOSSARY.md
```

## 全局词表与出处

`GLOSSARY.md` 与 `entities.yaml` 是**全局**的，不按 collection 切开：

- 同一个术语可以在多个 collection 的源文档里出现，证据按来源分别记录；
- 每条 evidence 都带 `collection`，注册表条目带 `collections` 列表；
- 页面 order 仍落在证据最多的那个 collection，但实体身份是全局的；
- 查一个词的全部出处：

```bash
"$WIKI_PYTHON" -m wikilib.cli lexicon provenance VPC
```

跨 collection 统一抽取（一个候选池、共用词表）：

```bash
"$WIKI_PYTHON" -m wikilib.cli extract --global
"$WIKI_PYTHON" -m wikilib.cli review build --global
"$WIKI_PYTHON" -m wikilib.cli review apply --global
```

逐 collection 增量仍可用；它只是全局词表的一个分区视图，不是两套词表。

## S1 候选生成

输入：规范化快照（`data/raw/<collection>/`）、`GLOSSARY.md`、`entities.yaml`。

候选来源：

| 来源 | 说明 | 例 |
|---|---|---|
| 全大写缩写 | `[A-Z][A-Z0-9]{1,6}` | VPC、NAT、CNI、IRSA |
| 大写多词序列 | 2–4 个首字母大写词 | NAT Gateway、Gateway API |
| CamelCase | 内部含大写 | CoreDNS、OpenBao、agentgateway |
| 连字符术语 | `kube-proxy`、`istio-cni` | kube-proxy |
| 标题词 | 小节标题里的拉丁词组 | ztunnel、waypoint、Ingress |
| 词表种子 | GLOSSARY 的 canonical/alias/全称 | NAT Gateway、Kustomization |

每个候选记录：

- `mentions`：正文出现次数
- `doc_coverage`：出现在几篇源文档
- `in_heading`：是否出现在小节标题
- `definitional`：证据里是否有定义句（`is/means/是/指/用于`）
- `code_mentions` / `table_mentions`：只在代码或表格里的比例
- `evidence[]`：最多 5 条，含 doc、heading、quote、offset、是否定义句

排序分数：

```
min(docs,6)*2 + sqrt(min(mentions,100))*0.8 + 3*in_heading
+ min(definitional,3) + (2+ headings) + 4*(词表已登记)
- 2*(代码占比>0.7) - 1.5*(名称长度<=2)
```

词表里登记过的条目**无条件进入复核**，不会被高频噪音挤出前 N 条。

## S2 确定性门禁

| 代号 | 条件 | 结果 |
|---|---|---|
| G1 | 命中现有页 title/slug/alias | `covered`，不送复核 |
| G2 | 与现有页名互为子串/超串 | `needs_review`（near_duplicate） |
| G3 | 证据的 doc+heading 不存在 | 拒绝（当前由快照生成，正常不会触发） |
| G4 | 全部证据在代码/表格，且单篇、非标题、非词表 | `observation` |
| G4b | 全部证据在代码/表格，但多篇/标题/词表 | `needs_review`（code_or_table_only） |
| G5 | 提及 <2、不在标题、不在词表 | `observation` |
| G6 | 全大写缩写未登记全称 | `needs_review`（acronym_unexpanded） |
| G7 | 与注册表类型冲突 | `needs_review`（type_conflict） |
| G8 | 候选是现有页别名的严格上位词 | `needs_review`（alias_hypernym） |
| G9 | 与 ≥2 个现有页存在包含关系 | `needs_review`（multi_collision） |

`observation` 不送 LLM，但保留在 `_candidates.json`，下一轮语料变化后重新评估。

## S3/S4 对抗复核

### 隔离

- Proposer 读 `_propose.json`（候选 + 证据 + 词表 + 现有页）。
- Challenger 读 `_challenge.json`（提案字段 + 证据 + 攻击清单），
  **不含** proposer 的 confidence、理由、proposed_by。
- Arbiter 只在双方结论不一致时介入。
- 三个角色使用独立上下文；最好不同模型档位（Proposer Haiku/Sonnet，Challenger Sonnet/Opus）。

### 12 项攻击清单

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

### 判定

| action | 含义 |
|---|---|
| `accept_new` | 新建独立页 |
| `merge_into` | 并入现有页 |
| `split` | 拆分现有页（默认交人确认边界） |
| `alias_only` | 只加别名 |
| `reject` | 不建页 |
| `defer` | 证据不足/冲突未决 |

## S6 合并规则

优先级从高到低：

1. Arbiter 的裁决
2. Challenger `verdict=revise` 的修订提案
3. Challenger `verdict=uphold` 且无 blocker → Proposer
4. 其余 → 人审队列

以下情况**强制**进人审，不论置信度：

- 缺 proposer 或 challenger
- challenger `defer` 或 `reject` 且无 arbiter
- 有未解决的 blocker
- 最终置信度 <0.7
- `merge_into` 目标不存在
- `concept` 只有单篇来源（thin_evidence）
- 缩写仍未展开
- 上位别名未给 qualifier
- `split`（边界需要人确认）
- 同名类型冲突

## Evidence packet（证据包）

每个 `mode:new` 的 page order 现在带一个 `evidence[]`：

```json
{
  "evidence": [
    {
      "collection": "tech-architecture",
      "doc": "AWS项目部署相关知识（修订版 v2.0）",
      "doc_id": "AWS项目部署相关知识（修订版 v2.0）",
      "heading": "2.3.2 VPC CNI",
      "heading_path": "...",
      "quote": "……",
      "offset": 1234,
      "definitional": true,
      "in_code": false,
      "in_table": false
    }
  ],
  "evidence_policy": {
    "purpose": "写页时优先使用这些证据片段；不要通读整篇源文档。",
    "rules": ["每条事实性断言必须来自 evidence[].quote；不得引入片段之外的事实。", "..."]
  }
}
```

- 最多 12 条，quote 截到 500 字，按候选证据的优先级（定义句、散文、非代码）排序；
- 每条带 `collection`，跨 collection 的实体也能追溯；
- 写页时**先读 evidence**，需要更多上下文时只读 `doc#heading`，不读整篇。

`update` 的 recompile order 也会自动生成 `evidence[]`：按页面已有的
`sources[].cites[]` 从 `data/raw/<collection>/<doc_id>.md` 抽 1–2 句定义性段落。
默认开启；需要关闭时加 `update --no-evidence`。

对已经生成、还没有证据包的 order：

```bash
"$WIKI_PYTHON" -m wikilib.cli review evidence
# 单个 collection：
"$WIKI_PYTHON" -m wikilib.cli review evidence --collection tech-architecture
```

证据来源优先 `_resolved.json`，缺失时回退到 `extract/_candidates.json`；
已有证据包默认跳过，`--force` 覆盖。

## 人审工作台（可复用）

把当前人审队列渲染成一个可勾选、可备注的 HTML 页面：

```bash
"$WIKI_PYTHON" -m wikilib.cli review html
# 默认 → data/review/human_review.html
# 单个 collection：
"$WIKI_PYTHON" -m wikilib.cli review html --collection tech-architecture
# 指定输出：
"$WIKI_PYTHON" -m wikilib.cli review html --out /tmp/review.html
```

页面能力：

- 按 collection / 原因 / 状态筛选，搜索名称、candidate_id 或证据；
- 每条可选 `accept_new / merge_into / alias_only / reject / defer`，补 canonical、type、aliases、merge_into、qualifier 与备注；
- “已复核”标记与备注存浏览器 localStorage，刷新/重新发布不丢；
- “生成决策 JSON” / “复制 JSON” / “导入 JSON”，支持断点续审；
- 导出格式固定为 `docs/human_decisions.schema.json`。

回灌：

```bash
"$WIKI_PYTHON" -m wikilib.cli review human-apply --file <导出的 JSON>
```

`human-apply` 会：

- 按 `collection` 分组，只处理 `reviewed: true` 的条目；
- 重新走一遍注册表合并、同名/别名冲突、建页 order；
- 保留本次未导入的人审项，不会因为它们没出现在导出文件里就被清空。

页面是**重新生成**的，不是一次性文件：每次 `review apply` 或人审后，重跑 `review html` 即可得到最新队列。

> `templates/human_review.template.html` 只是模板，不能直接打开；请打开生成后的
> `data/review/human_review.html`。`review html --open` 会直接调起浏览器。

## 人审队列

`data/review/<collection>/_human_queue.md` 按原因分组，每条给出：

- candidate_id、surface
- 提案（action / canonical / type / confidence）
- 触发原因
- 反对意见（rule / severity / evidence）
- 2 条证据
- 可选处理

处理完必须回写 decisions 并重跑 `review apply`；不要直接改页面，
否则注册表不更新，下一轮会重复问。

## 注册表 `entities.yaml`

唯一事实源，人可编辑。字段：

```yaml
- canonical: NAT Gateway
  type: entity
  collection: tech-architecture
  aliases: [NAT 网关]
  acronym_of: ""
  qualifier: ""
  status: active          # active | merged | rejected | deferred
  slug: nat-gateway
  evidence: [...]
  decisions: [...]
```

`status=merged` 保留并入记录，便于回滚；`rejected` 记录明确不建页的原因。

## 词汇表 `GLOSSARY.md`

按 `##` 分节：

- `## 概念`：英文 / 中文 / 禁用译法
- `## 专有名词`：名称 / 类型 / 中文说明 / 别名
- `## 缩写`：缩写 / 全称 / 中文 / 说明
- `## 通用缩写（不建页）`：API/HTTP/TCP 这类只用于展开、不建页的缩写
- `## 保留英文`：不翻译的词
- `## 已知劣质译法`：禁止写法

同一名称可以同时出现在专有名词和缩写里，加载时按任意写法合并。
`lexicon check` 会报告写法冲突与缺全称；`lexicon missing` 扫描快照里未登记的缩写。

## 验收线

| 指标 | 通过线 |
|---|---|
| 人审率 | ≤10% |
| 新增低价值/重复页 | ≤5% |
| `lint --strict` | 0 错误 |
| `registry check` | 0 冲突 |
| `lexicon check` | 0 冲突、0 缺全称 |
