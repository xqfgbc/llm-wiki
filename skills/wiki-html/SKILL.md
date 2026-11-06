---
name: wiki-html
description: |
  打开 wiki 的网页站点。先增量生成，再在浏览器里打开；
  页面改过就只重写改动的那几页，没改动的跳过。
  站点是整站产物，包含所有 collection，可离线浏览。
tags: [wiki, html, site, 浏览]
---

# 打开 Wiki 站点 (wiki-html)

## 项目路径配置

```bash
source "$HOME/.claude/skills/wiki-html/.env"
cd "$WIKI_DIR"
```

```bash
# .env
export WIKI_DIR=/path/to/llm-wiki
export WIKI_PYTHON=/path/to/.venvs/ai-wiki/bin/python3
```

## 工作流程

### Step 1: 生成

```bash
"$WIKI_PYTHON" -m wikilib.cli html
```

输出形如：

```
站点 data/site（3 个 collection）
页 32 · 新写 2 · 未变跳过 30 · 清理残留 0 · 图片 0 · 未建页 0
```

- **新写 0 / 跳过 N** 是正常的：内容没变就不重写文件，避免 mtime 抖动
- **清理残留** 大于 0 表示有页面被改名或删除，旧 html 已收走
- 生成耗时通常在 0.3 秒量级

### Step 2: 打开

```bash
open "$WIKI_DIR/data/site/index.html"
```

想直接打开某一页：

```bash
open "$WIKI_DIR/data/site/<slug>.html"
```

slug 与文件名不同——它是 frontmatter 里的 `slug` 字段。不确定时先看首页左侧导航，
或者：

```bash
ls "$WIKI_DIR/data/site"/*.html
```

### Step 3: 告诉用户看什么

- 左侧按 collection → 类型分组；**窄屏**时导航收起，搜索栏移到顶部
- 顶部状态栏：页数 / 陈旧 / 争议 / 未建页 / 内容 token 累计
- 右侧：反向链接与来源索引，引用可点回 Obsidian 原文
- 首页底部有「Token 用量」，只统计内容产出

## 常用参数

```bash
"$WIKI_PYTHON" -m wikilib.cli html --open                  # 生成并打开
"$WIKI_PYTHON" -m wikilib.cli html --collection ontology   # 只渲染某个 collection
```

> 注意：`--collection` 会让站点只包含该 collection，导航与首页也随之收窄。
> 平时不要加，站点默认是整站。

## 注意事项

- 站点在 `data/site/`，是派生物，删掉重跑即可
- 内容是**离线**可用的：不依赖 CDN、不需要后端；搜索走内联索引
- 如果页面里 `lint` 报红链，站点上对应链接就是红色——那是知识缺口，不是渲染错误
- 引用点开后跳 Obsidian；图片按内容哈希复制，同名不同图不会串
- 打不开或样式错乱时，先确认 `data/site/assets/style.css` 与 `app.js` 是否存在
