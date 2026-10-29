"""Публичная страница руководства поддержки: /support-guide.

Источник — app/web/guide/support-guide.md (правится как обычный текст), оформление — app/web/guide/page.html.
Markdown разбирается здесь же (заголовки, абзацы, списки, таблицы, цитаты-шаблоны, **жирный**, `код`, ссылки):
## — раздел с номером, ### — пункт (по пунктам ищет поиск на странице), > — шаблон ответа с кнопкой «Скопировать».
Страница пересобирается, когда меняется любой из двух файлов."""

import html
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
DIR = Path(__file__).parent / "guide"
SOURCE, PAGE = DIR / "support-guide.md", DIR / "page.html"
_cache: dict[str, object] = {"key": None, "html": ""}


def _inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(https?://[^\s<)]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t


def _blocks(lines: list[str]) -> list[tuple[str, object]]:
    blocks, i, n = [], 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s or s == "---":
            i += 1
            continue
        if m := re.match(r"^(#{2,3})\s+(.*)$", s):
            blocks.append(("h" + str(len(m.group(1))), m.group(2)))
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append(("table", rows))
            continue
        if s.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(("quote", quote))
            continue
        if m := re.match(r"^(-|\d+\.)\s+(.*)$", s):
            kind, items = ("ul" if m.group(1) == "-" else "ol"), []
            while i < n:
                cur = lines[i]
                mm = re.match(r"^(-|\d+\.)\s+(.*)$", cur.strip())
                if mm and not cur.startswith("  "):
                    items.append(mm.group(2))
                elif cur.startswith("  ") and cur.strip() and items:
                    items[-1] += " " + cur.strip()  # продолжение пункта на следующей строке
                else:
                    break
                i += 1
            blocks.append((kind, items))
            continue
        para = [s]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#|\||>|-\s|\d+\.\s|---)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(para)))
    return blocks


def _block_html(kind: str, data) -> str:
    if kind == "p":
        return f"<p>{_inline(data)}</p>"
    if kind in ("ul", "ol"):
        return f"<{kind}>" + "".join(f"<li>{_inline(x)}</li>" for x in data) + f"</{kind}>"
    if kind == "table":
        head = data[0]
        rows = [r for r in data[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
        return ('<div class="tablewrap"><table><thead><tr>' + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                + "</tbody></table></div>")
    if kind == "quote":
        return (f'<figure class="tpl"><blockquote>{"<br>".join(_inline(x) for x in data)}</blockquote>'
                '<button type="button" class="copy">Скопировать</button></figure>')
    return ""


def render(source: str, page: str) -> str:
    lines = source.splitlines()
    title = lines[0].lstrip("# ").strip()
    intro, body, toc = [], [], []
    sec = sub = 0
    open_item = open_sec = False
    for kind, data in _blocks(lines[1:]):
        if kind == "h2":
            if open_item:
                body.append("</div>")
            if open_sec:
                body.append("</section>")
            sec, sub = sec + 1, 0
            num = m.group(1) if (m := re.match(r"^(\d+)\.", data)) else ""
            label = re.sub(r"^\d+\.\s*", "", data)
            toc.append((f"s{sec}", label, num, []))
            body.append(f'<section class="sec" id="s{sec}"><h2><span class="num">{num}</span>{_inline(label)}</h2>'
                        '<div class="item lead-item">')
            open_sec = open_item = True
        elif kind == "h3":
            if open_item:
                body.append("</div>")
            sub += 1
            toc[-1][3].append((f"s{sec}-{sub}", data))
            body.append(f'<div class="item" id="s{sec}-{sub}"><h3>{_inline(data)}</h3>')
            open_item = True
        else:
            (body if sec else intro).append(_block_html(kind, data))
    if open_item:
        body.append("</div>")
    if open_sec:
        body.append("</section>")
    toc_html = []
    for sid, label, num, subs in toc:
        toc_html.append(f'<li><a href="#{sid}"><span class="num">{num}</span>{_inline(label)}</a>')
        if subs:
            toc_html.append("<ul>" + "".join(f'<li><a href="#{i}">{_inline(t)}</a></li>' for i, t in subs) + "</ul>")
        toc_html.append("</li>")
    return (page.replace("{{TITLE}}", html.escape(title)).replace("{{INTRO}}", "\n".join(intro))
            .replace("{{TOC}}", "\n".join(toc_html)).replace("{{BODY}}", "\n".join(body)))


def page_html() -> str:
    key = (SOURCE.stat().st_mtime_ns, PAGE.stat().st_mtime_ns)
    if _cache["key"] != key:
        _cache["html"] = render(SOURCE.read_text(encoding="utf-8"), PAGE.read_text(encoding="utf-8"))
        _cache["key"] = key
    return _cache["html"]


@router.api_route("/support-guide", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def support_guide():
    return HTMLResponse(page_html(), headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})
