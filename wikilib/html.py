"""静态站点：把 wiki 页渲染成可离线打开的三栏 HTML。

三个转换必须在这里做对
----------------------
1. `【[[文档#小节]]】` → Obsidian 深链。锚点用**原始标题**（源文件里存在的那个）。
2. `[[页面]]` → 站内链接；指向源文档的 → 深链；其余 → 红链（未建页）。
3. `![[图片]]` → `assets/img/<sha8>.<ext>`，按内容去重。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from markdown_it import MarkdownIt

from . import contracts
from .pages import deep_link, page_paths

CITE_RE = re.compile(r"【\[\[([^\[\]#]+)#([^\[\]]+)\]\]】")
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#([^\[\]|]+))?(?:\|([^\[\]]*))?\]\]")
IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
TEMPLATE_VERSION = 2

CSS = """
:root{--bg:#fbfbfa;--fg:#1f2328;--muted:#656d76;--line:#d8dee4;--card:#fff;
--accent:#1a5fb4;--red:#c01c28;--yellow:#9a6700;--grey:#8c959f}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6edf3;--muted:#8b949e;
--line:#30363d;--card:#161b22;--accent:#6cb6ff;--red:#ff7b72;--yellow:#d29922;--grey:#6e7681}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.75 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{display:grid;grid-template-columns:230px minmax(0,1fr) 240px;gap:0;
max-width:1400px;margin:0 auto;min-height:100vh}
nav,aside{padding:20px 16px;border-right:1px solid var(--line);position:sticky;
top:0;height:100vh;overflow:auto;font-size:13.5px}
aside{border-right:none;border-left:1px solid var(--line)}
main{padding:28px 40px 80px;min-width:0}
h1{font-size:26px;margin:.2em 0 .6em;line-height:1.35}
h2{font-size:19px;margin:1.9em 0 .6em;padding-bottom:.25em;
border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:1.4em 0 .4em}
table{border-collapse:collapse;width:100%;margin:1em 0;font-size:14px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:rgba(127,127,127,.08)}
blockquote{margin:1em 0;padding:.6em 1em;border-left:3px solid var(--accent);
background:rgba(127,127,127,.07);border-radius:0 4px 4px 0}
code{background:rgba(127,127,127,.14);padding:.12em .35em;border-radius:4px;
font-size:.9em;font-family:ui-monospace,Menlo,monospace}
pre{background:rgba(127,127,127,.12);padding:12px 14px;border-radius:6px;overflow:auto}
pre code{background:none;padding:0}
img{max-width:100%;border:1px solid var(--line);border-radius:6px;margin:.6em 0}
.cite{font-size:12.5px;color:var(--muted);white-space:nowrap}
.cite:hover{color:var(--accent)}
.redlink{color:var(--red);border-bottom:1px dotted var(--red)}
.nav-group{margin-bottom:18px}
.nav-title{font-size:11.5px;letter-spacing:.08em;color:var(--muted);
text-transform:uppercase;margin-bottom:6px}
.nav a{display:block;padding:3px 0;color:var(--fg)}
.nav a.on{color:var(--accent);font-weight:600}
.badge{font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid var(--line);
color:var(--muted);margin-left:6px}
.stale{color:var(--grey);text-decoration:line-through}
.disputed{color:var(--yellow)}
.top{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);
border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:8px}
.back a,.src a{display:block;padding:3px 0;font-size:13px}
.src{margin-bottom:10px}
.src .t{font-size:12.5px;color:var(--muted)}
.reds a{display:block;font-size:13px;color:var(--red)}
#q{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);font-size:13px;margin-bottom:14px}
#q:focus{outline:2px solid var(--accent);outline-offset:1px}
#hits{display:none;position:fixed;left:16px;top:66px;width:310px;max-height:62vh;
overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;
box-shadow:0 10px 30px rgba(0,0,0,.22);z-index:60;padding:6px}
#hits.on{display:block}
#hits .cnt{font-size:11.5px;color:var(--muted);padding:4px 8px 6px}
#hits a{display:block;padding:6px 8px;border-radius:5px;font-size:13.5px;color:var(--fg)}
#hits a:hover,#hits a.sel{background:rgba(127,127,127,.14)}
#hits a small{color:var(--muted);font-size:11.5px;margin-left:6px}
#hits mark{background:rgba(108,182,255,.35);color:inherit;border-radius:2px}
@media(max-width:1000px){.wrap{grid-template-columns:1fr}
nav,aside{display:none}main{padding:20px}}
"""

APP_JS = r"""
const idx=window.__WIKI_INDEX__||{pages:[]};
const q=document.getElementById('q'),hits=document.getElementById('hits');
const TYPES={concept:'概念',entity:'实体',topic:'主题',note:'笔记'};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hl=(t,s)=>esc(t).replace(new RegExp('('+s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi'),'<mark>$1</mark>');
function terms(s){
  const t=s.toLowerCase().trim(),out=new Set();
  for(const w of t.split(/\s+/))if(w)out.add(w);
  if(!out.size)out.add(t);
  return [...out];
}
function score(p,s){
  const ts=terms(s);let n=0;
  for(const t of ts){
    if(p.title.toLowerCase().includes(t))n+=10;
    for(const a of(p.aliases||[]))if(a.toLowerCase().includes(t))n+=8;
    for(const h of(p.headings||[]))if(h.toLowerCase().includes(t))n+=5;
    if((p.text||'').toLowerCase().includes(t))n+=1;
  }
  return n;
}
let sel=-1,items=[];
function render(){
  const s=q.value.trim();
  if(!s){hits.className='';hits.innerHTML='';items=[];sel=-1;return;}
  const r=idx.pages.map(p=>({p,n:score(p,s)})).filter(x=>x.n>0).sort((a,b)=>b.n-a.n).slice(0,12);
  items=r;sel=-1;hits.className='on';
  if(!r.length){hits.innerHTML='<div class="cnt">没有匹配「'+esc(s)+'」</div>';return;}
  hits.innerHTML='<div class="cnt">'+r.length+' 条结果 · ↑↓ 选择，回车打开，Esc 关闭</div>'
    +r.map((x,i)=>`<a href="${x.p.slug}.html" data-i="${i}">${hl(x.p.title,s)}<small>${TYPES[x.p.type]||x.p.type}</small></a>`).join('');
}
q.addEventListener('input',render);
q.addEventListener('focus',()=>{if(q.value.trim())render();});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){q.value='';render();q.blur();return;}
  if(!items.length)return;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    sel=(sel+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;
    [...hits.querySelectorAll('a')].forEach((a,i)=>a.classList.toggle('sel',i===sel));
  }
  if(e.key==='Enter'&&sel>=0){location.href=items[sel].p.slug+'.html';}
});
document.addEventListener('click',e=>{
  if(!hits.contains(e.target)&&e.target!==q){hits.className='';}
});
"""





@dataclass
class RenderStats:
    pages: int = 0
    written: int = 0
    skipped: int = 0
    images: int = 0
    red_links: int = 0
    pruned: int = 0


class Site:
    def __init__(self, cfg, collection: str, snap):
        self.cfg = cfg
        self.collection = collection
        self.snap = snap
        self.md = MarkdownIt("commonmark", {"html": True, "linkify": False})
        self.md.enable("table").enable("strikethrough")

        root = os.path.join(cfg.obsidian.wiki_root, collection)
        self.pages = [contracts.parse_page(p) for p in page_paths(root)]
        self.by_name: Dict[str, contracts.Page] = {}
        for p in self.pages:
            self.by_name[contracts.norm_name(p.title)] = p
            self.by_name[contracts.norm_name(p.slug)] = p
            for a in p.aliases():
                self.by_name.setdefault(contracts.norm_name(a), p)

        self.cite_map, self.origin_map = self._build_cite_map()
        self.images: Dict[str, str] = {}
        self.red: Dict[str, int] = {}

    # ------------------------------------------------------------ 引用映射
    def _build_cite_map(self) -> Tuple[Dict[Tuple[str, str], Dict], Dict[str, str]]:
        cmap, origin = {}, {}
        for page in self.pages:
            for src in page.source_docs():
                doc_id, _, _ = self.snap.resolve(src.get("doc", ""), "")
                doc_id = doc_id or src.get("doc", "")
                abs_origin = self.snap.origins.get(doc_id, "")
                rel = (os.path.relpath(abs_origin, self.cfg.obsidian.vault_root)
                       .replace(os.sep, "/")) if abs_origin else ""
                origin[doc_id] = rel
                for c in (src.get("cites") or []):
                    h = str(c.get("heading", ""))
                    orig = (self.snap.docs.get(doc_id, {}).get(contracts.norm_name(h)) or h)
                    cmap[(contracts.norm_name(doc_id), contracts.norm_name(h))] = {
                        "doc_id": doc_id, "heading": h, "orig_heading": orig,
                        "origin_path": rel,
                    }
        return cmap, origin

    # ------------------------------------------------------------ 变换
    def _images_to_html(self, text: str, page: contracts.Page,
                        site_dir: str) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1).strip()
            url = self._resolve_image(name, page)
            if not url:
                return '<span class="redlink">［图片缺失：%s］</span>' % html_mod.escape(name)
            return "![%s](%s)" % (name, url)
        return IMAGE_RE.sub(repl, text)

    def _resolve_image(self, name: str, page: contracts.Page) -> str:
        if not self.images:
            return ""
        return self.images.get(name, "")

    def _doc_id_of(self, ref: str) -> str:
        """用标题或文件名都能解析到 doc_id —— 页面里两种写法都有。"""
        doc_id, _, _ = self.snap.resolve(ref, "")
        return doc_id or ref

    def _citations_to_md(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            doc, heading = m.group(1).strip(), m.group(2).strip()
            info = self.cite_map.get((contracts.norm_name(self._doc_id_of(doc)),
                                      contracts.norm_name(heading)))
            label = "【%s】" % _short_cite(doc, heading)
            full = "%s › %s" % (doc, heading)
            if not info or not info["origin_path"]:
                return '<span class="cite" title="%s">%s</span>' % (
                    html_mod.escape(full), html_mod.escape(label))
            url = deep_link(self.cfg.obsidian.vault_name, info["origin_path"],
                            info["orig_heading"])
            return '[%s](%s "%s")' % (label,
                                      urllib.parse.quote(url, safe=":/?&=%#"),
                                      full.replace('"', "'"))
        return CITE_RE.sub(repl, text)

    def _wikilinks_to_html(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            target, anchor, alias = m.group(1).strip(), m.group(2), m.group(3)
            label = alias or target
            key = contracts.norm_name(target)
            page = self.by_name.get(key)
            if page is not None:
                return '<a href="%s.html">%s</a>' % (page.slug, html_mod.escape(label))
            if anchor:
                info = self.cite_map.get((contracts.norm_name(self._doc_id_of(target)),
                                          contracts.norm_name(anchor)))
                if info and info["origin_path"]:
                    url = deep_link(self.cfg.obsidian.vault_name, info["origin_path"],
                                    info["orig_heading"])
                    return '<a class="cite" href="%s">%s</a>' % (
                        html_mod.escape(url), html_mod.escape(label))
            doc_id = self._doc_id_of(target)
            if doc_id and doc_id in self.origin_map:
                url = deep_link(self.cfg.obsidian.vault_name, self.origin_map[doc_id])
                return '<a href="%s">%s</a>' % (html_mod.escape(url),
                                                html_mod.escape(label))
            self.red[target] = self.red.get(target, 0) + 1
            return '<a class="redlink" title="尚未建页">%s</a>' % html_mod.escape(label)
        return WIKILINK_RE.sub(repl, text)

    def render_body(self, page: contracts.Page, site_dir: str) -> str:
        text = self._images_to_html(page.body, page, site_dir)
        text = self._citations_to_md(text)
        text = self._wikilinks_to_html(text)
        rendered = self.md.render(text)
        # Markdown 链接带不上 class，渲染后统一给「引用」和「源文档」标注样式
        rendered = re.sub(r'<a href="(obsidian://[^"]*)">(【[^<]*】)</a>',
                          r'<a class="cite" href="\1">\2</a>', rendered)
        return rendered

    # ------------------------------------------------------------ 输出
    def build(self) -> RenderStats:
        stats = RenderStats()
        site_dir = self.cfg.site_dir
        os.makedirs(os.path.join(site_dir, "assets", "img"), exist_ok=True)

        self._prepare_images(site_dir)

        nav = self._nav_html(None)
        for page in self.pages:
            stats.pages += 1
            body = self.render_body(page, site_dir)
            aside = self._aside_html(page)
            doc_html = self._layout(page.title, nav, body, aside,
                                    self._top_html())
            if self._write_if_changed(os.path.join(site_dir, page.slug + ".html"),
                                      doc_html):
                stats.written += 1
            else:
                stats.skipped += 1

        self._write(os.path.join(site_dir, "index.html"),
                    self._layout("Agent 安全 Wiki", self._nav_html("index"),
                                 self._home_html(), self._reds_html(),
                                 self._top_html()))
        self._write(os.path.join(site_dir, "assets", "style.css"), CSS)
        stats.pruned = self._prune(site_dir)
        self._write(os.path.join(site_dir, "assets", "app.js"), APP_JS)
        stats.images = len(set(self.images.values()))
        stats.red_links = len(self.red)
        return stats

    def _prune(self, site_dir: str) -> int:
        """删掉改名/删页后残留的 html，避免站点里挂着已不存在的页。"""
        keep = {p.slug + ".html" for p in self.pages} | {"index.html"}
        removed = 0
        for name in os.listdir(site_dir):
            if name.endswith(".html") and name not in keep:
                os.remove(os.path.join(site_dir, name))
                removed += 1
        return removed

    def _prepare_images(self, site_dir: str) -> None:
        """按 sha256 命名复制，同名不同内容也不会串。"""
        seen: Dict[str, str] = {}
        out_dir = os.path.join(site_dir, "assets", "img")
        for page in self.pages:
            for m in IMAGE_RE.finditer(page.body):
                name = m.group(1).strip()
                src = self._find_image_source(name)
                if not src:
                    continue
                digest = hashlib.sha256(open(src, "rb").read()).hexdigest()[:8]
                ext = os.path.splitext(src)[1].lower() or ".png"
                target = "%s%s" % (digest, ext)
                if target not in seen:
                    shutil.copyfile(src, os.path.join(out_dir, target))
                    seen[target] = src
                self.images[name] = "assets/img/" + target

    def _find_image_source(self, name: str) -> Optional[str]:
        for root in [os.path.dirname(p) for p in self.snap.origins.values()]:
            cand = os.path.join(root, name)
            if os.path.exists(cand):
                return cand
        # 名字对不上时按时间戳回退（源里存在「粘贴图像<ts>」而文件叫「Pasted image <ts>」）
        digits = re.search(r"\d{8,}", name)
        if not digits:
            return None
        for root in [os.path.dirname(p) for p in self.snap.origins.values()]:
            for f in os.listdir(root):
                if digits.group(0) in f and f.lower().endswith((".png", ".jpg", ".jpeg")):
                    return os.path.join(root, f)
        return None

    def _write_if_changed(self, path: str, content: str) -> bool:
        if os.path.exists(path):
            if open(path, encoding="utf-8").read() == content:
                return False
        self._write(path, content)
        return True

    def _write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # ------------------------------------------------------------ 片段
    def _top_html(self) -> str:
        stale = sum(1 for p in self.pages if p.status == "stale")
        disputed = sum(1 for p in self.pages if p.status == "disputed")
        return ('<div class="top"><span>%d 页</span><span>陈旧 %d</span>'
                '<span>争议 %d</span><span>未建页 %d</span>'
                '<span>%s</span></div>'
                % (len(self.pages), stale, disputed, len(self.red), self.collection))

    def _nav_html(self, current: Optional[str]) -> str:
        groups: Dict[str, List[contracts.Page]] = {}
        for p in self.pages:
            groups.setdefault(p.type or "other", []).append(p)
        labels = {"concept": "概念", "entity": "实体", "topic": "主题", "note": "笔记"}
        out = ['<input id="q" placeholder="搜索…" autocomplete="off"><div id="hits"></div>']
        for typ in ("concept", "entity", "topic", "note"):
            if typ not in groups:
                continue
            out.append('<div class="nav-group"><div class="nav-title">%s</div>'
                       % labels.get(typ, typ))
            for p in sorted(groups[typ], key=lambda x: x.title):
                cls = ' class="on"' if current == p.slug else ""
                badge = ""
                if p.status == "stale":
                    badge = '<span class="badge">陈旧</span>'
                elif p.status == "disputed":
                    badge = '<span class="badge">争议</span>'
                out.append('<a%s href="%s.html">%s%s</a>'
                           % (cls, p.slug, html_mod.escape(p.title), badge))
            out.append("</div>")
        return "".join(out)

    def _aside_html(self, page: contracts.Page) -> str:
        inbound = []
        for other in self.pages:
            if other.slug == page.slug:
                continue
            for m in WIKILINK_RE.finditer(other.body):
                if contracts.norm_name(m.group(1)) in (
                        contracts.norm_name(page.title), contracts.norm_name(page.slug)):
                    inbound.append(other)
                    break
        parts = ['<div class="nav-title">反向链接</div><div class="back">']
        parts += ['<a href="%s.html">%s</a>' % (p.slug, html_mod.escape(p.title))
                  for p in inbound] or ['<span style="color:var(--muted)">无</span>']
        parts.append("</div>")

        parts.append('<div class="nav-title" style="margin-top:20px">来源与引用</div>')
        parts.append('<div class="src">')
        for src in page.source_docs():
            doc_id, _, _ = self.snap.resolve(src.get("doc", ""), "")
            rel = self.origin_map.get(doc_id or "", "")
            title = doc_id or src.get("doc", "")
            if rel:
                parts.append('<a class="back" href="%s">%s</a>'
                             % (html_mod.escape(deep_link(self.cfg.obsidian.vault_name, rel)),
                                html_mod.escape(title)))
            else:
                parts.append('<div style="font-size:13px">%s</div>' % html_mod.escape(title))
            for c in (src.get("cites") or []):
                h = str(c.get("heading", ""))
                info = self.cite_map.get((contracts.norm_name(doc_id),
                                          contracts.norm_name(h)))
                if info and info["origin_path"]:
                    parts.append('<a style="padding-left:12px;font-size:12px"'
                                 ' href="%s">› %s</a>'
                                 % (html_mod.escape(deep_link(
                                     self.cfg.obsidian.vault_name,
                                     info["origin_path"], info["orig_heading"])),
                                    html_mod.escape(h[:28])))
        parts.append("</div>")
        return "".join(parts)

    def _reds_html(self) -> str:
        if not self.red:
            return '<div class="nav-title">未建页</div><span style="color:var(--muted)">无</span>'
        items = sorted(self.red.items(), key=lambda kv: -kv[1])
        return ('<div class="nav-title">未建页（知识缺口）</div><div class="reds">'
                + "".join('<a>%s ×%d</a>' % (html_mod.escape(k), v) for k, v in items)
                + "</div>")

    def _home_html(self) -> str:
        rows = []
        for typ, label in (("concept", "概念"), ("entity", "实体"), ("topic", "主题")):
            items = [p for p in self.pages if p.type == typ]
            if not items:
                continue
            rows.append("<h2>%s（%d）</h2><ul>" % (label, len(items)))
            for p in sorted(items, key=lambda x: x.title):
                rows.append('<li><a href="%s.html">%s</a> — %s</li>'
                            % (p.slug, html_mod.escape(p.title),
                               html_mod.escape(_one_liner(p))))
            rows.append("</ul>")
        label = next((c.label for c in self.cfg.collections
                      if c.name == self.collection), self.collection)
        return '<h1>%s</h1>%s' % (html_mod.escape(label), "".join(rows))

    def _layout(self, title: str, nav: str, body: str, aside: str, top: str) -> str:
        return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>%s</title><link rel=\"stylesheet\" href=\"assets/style.css\"></head>"
                "<body><div class=\"wrap\"><nav class=\"nav\">%s</nav>"
                "<main>%s%s</main><aside>%s</aside></div>"
                "<script>window.__WIKI_INDEX__=%s</script>"
                "<script src=\"assets/app.js\"></script></body></html>"
                % (html_mod.escape(title), nav, top, body, aside,
                   self._search_json()))

    def _search_json(self) -> str:
        pages = []
        for p in self.pages:
            text = re.sub(r"```.*?```", " ", p.body, flags=re.S)
            text = re.sub(r"【\[\[.*?\]\]】", " ", text)
            text = re.sub(r"\[\[.*?\]\]", " ", text)
            pages.append({
                "slug": p.slug, "title": p.title, "aliases": p.aliases(),
                "type": p.type, "status": p.status,
                "headings": p.sections, "text": re.sub(r"\s+", " ", text)[:3000],
            })
        return json.dumps({"v": TEMPLATE_VERSION, "pages": pages}, ensure_ascii=False)



_COMMON_PREFIXES = (
    "Credential Brokering Patterns for AI Agents ",
    "Defense in depth for autonomous AI agents",
    "AI Agent 安全最佳实践 2026：从业界共识到工程落地",
)


def _short_cite(doc: str, heading: str) -> str:
    """引用标签只保留可辨认的最短形式；完整名称放在 title 属性里。"""
    short = doc
    for prefix in _COMMON_PREFIXES:
        if short.startswith(prefix):
            short = short[len(prefix):] or prefix
            break
    short = short.replace(": Don’t Give the Agent the Keys", "").strip() or doc[:16]
    if len(short) > 16:
        short = short[:15] + "…"
    h = heading.replace("Credential Brokering 4 AI Agents (CB4A)", "CB4A")
    if len(h) > 20:
        h = h[:19] + "…"
    return "%s › %s" % (short, h)


def _one_liner(page: contracts.Page) -> str:
    """索引页的一句话摘要：取开头的引用块，去掉 Markdown 标记与引用，截在句末。"""
    for line in page.body.split("\n"):
        s = line.strip()
        if not s.startswith(">"):
            continue
        s = s.lstrip("> ").replace("**一句话**：", "").replace("**", "")
        s = re.sub(r"【\[\[.*?\]\]】", "", s).strip()
        if not s:
            continue
        if len(s) > 90:
            cut = max(s.rfind("。", 0, 90), s.rfind("；", 0, 90))
            s = s[:cut + 1] if cut > 30 else s[:88] + "…"
        return s
    return ""
