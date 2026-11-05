# llm-wiki

把指定的 Obsidian 目录编译成**中文** wiki 页，产物同时供 LLM 消费（Markdown）和人阅读（HTML）。

与 `../AI-KG` 知识库是**两套独立系统**：

| | AI-KG | llm-wiki |
|---|---|---|
| 输入 | PPT / 报告 → 转换层 → Obsidian | 笔记 / 网页剪藏 / 任意指定目录 |
| 索引 | `kb.sqlite`（原文切片） | `data/wiki.sqlite`（原文切片 + wiki 页） |
| 产物 | 带页码的原文证据 | 跨文档归纳的中文概念页 |
| 关系 | 零代码耦合 | 零代码耦合 |

两者只在 `kg-qa` 查询时各被调用一次，结果在模型上下文里合并。

## 快速开始

```bash
# 环境（必须独立；AI-KG 的 venv 没有 pip）
uv venv ~/.venvs/ai-wiki
uv pip install --python ~/.venvs/ai-wiki/bin/python3 -r requirements.txt

# 配置
cp config.local.yaml.example config.local.yaml   # 填 vault 路径与 collection

# 首次：规范化 + 建索引 + 生成站点
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli normalize --dry-run   # 先看标题修复对照
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli build
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli html --open

# 之后：增量
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli update    # 产出 work order
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli build
~/.venvs/ai-wiki/bin/python3 -m wikilib.cli lint --strict
```

## 命令

| 命令 | 作用 |
|---|---|
| `normalize` | 源规范化：修标题断行、去剪藏噪音、修图片引用，写快照到 `data/raw/` |
| `build` | 建 `data/wiki.sqlite`（源切片 + wiki 页） |
| `update` | 增量：比对源 hash → 算受影响页 → 产出 work order |
| `lint` | 确定性校验（引用可解析 / 结构 / 中文占比 / 禁用译法 / 尺寸 / 冲突） |
| `html` | 增量生成静态站点到 `data/site/` |
| `query` | 检索，给 `kg-qa` 调用；失败返回空列表，退出码 0 |
| `status` | 概览与排除项自检 |
| `exclude list\|add\|why\|remove` | 排除名单维护 |

## 数据边界

- **仓库只保存代码**。`data/`、`config.local.yaml`、`*.sqlite` 全部在 `.gitignore` 里。
- 提交前自检：`git grep -nE '/(Users|home)/'` 必须为空。
- wiki 页写在 Obsidian vault（`obsidian/wiki/`），不进任何 git。

## 目录

```
wikilib/
├── config.py       配置加载（config.yaml + config.local.yaml）
├── exclusions.py   排除名单（四级粒度，两份文件合并）
├── normalize.py    源规范化 N1–N7
├── chunk.py        自适应切分（按标题层级，绝不切开代码块）
├── tokenize.py     jieba 预分词（与 AI-KG 同口径，独立实现）
├── index.py        建 wiki.sqlite
├── query.py        BM25 检索 + 查询形态判定
├── glossary.py     术语表：唯一译法 + 中→英查询扩展
├── contracts.py    页面契约：frontmatter / 引用 / 结构
├── verify.py       14 条确定性校验
├── pages.py        页面收集与深链
├── state.py        增量状态（源 hash / 页面依赖 / 运行记录）
├── plan.py         差异 → 影响面 → work order
├── index_md.py     index.md / log.md
├── html.py         静态站点
└── cli.py          命令行入口
```

## 设计要点

- **引用锚点用原始标题**：规范化会修好被剪藏破坏的标题，但 Obsidian 锚点必须匹配源文件里的真实文本，所以 `cites` 里同时记 `heading`（规范化）与 `orig_heading`（原始）。
- **触碰即整页重编**：不做补丁式追加，避免多轮之后结构漂移。
- **确定性规则决定能不能发布，LLM 只决定要不要人看**：引用解析、结构、中文占比全部纯 Python 判定，避免撰写与校验同模型互相背书。
- **跨语言召回靠术语表**：语料多为英文、提问多为中文，BM25 直接对不上；`GLOSSARY.md` 同时充当查询扩展词表。
