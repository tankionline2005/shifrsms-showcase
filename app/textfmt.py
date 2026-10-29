"""Текст с оформлением Telegram (HTML-разметка) из визуального редактора панели.

clean() оставляет только то, что понимает Telegram: жирный, курсив, подчёркнутый, зачёркнутый,
моноширинный, спойлер, ссылки и цитаты. Всё остальное (в том числе то, что вставили бы в обход
редактора) выбрасывается — текст остаётся. Сообщения каналов правят и рефералы, а открывает их и
админ, поэтому чистка — ещё и защита панели от чужого кода в тексте.
visible_len() — длина текста без тегов: так считает лимиты сам Telegram."""

import html
import re
from html.parser import HTMLParser

# тег в редакторе -> тег Telegram
TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u", "s": "s", "strike": "s",
        "del": "s", "code": "code", "pre": "pre", "a": "a", "tg-spoiler": "tg-spoiler", "blockquote": "blockquote"}
LINK_SCHEMES = ("http://", "https://", "tg://")


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str | None] = []  # открытые теги: тег Telegram или None (выброшенный)
        self.skip = 0  # внутри <script>/<style> — содержимое не нужно

    def _newline(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
            return
        if tag == "br":
            self.out.append("\n")
            return
        if tag in ("div", "p", "li"):  # блок — с новой строки
            self._newline()
        if tag == "span" and "tg-spoiler" in (a.get("class") or "").split():
            tag = "tg-spoiler"
        t = TAGS.get(tag)
        if t == "a":
            href = (a.get("href") or "").strip()
            if not href.lower().startswith(LINK_SCHEMES):
                t = None  # ссылка без адреса или javascript: — оставляем только текст
            else:
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("a")
                return
        if t:
            self.out.append(f"<{t}>")
        self.stack.append(t)

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "br" or not self.stack:
            return
        t = self.stack.pop()
        if t:
            self.out.append(f"</{t}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:  # незакрытые теги — закрываем
            t = self.stack.pop()
            if t:
                self.out.append(f"</{t}>")
        return "".join(self.out)


def clean(text: str) -> str:
    p = _Cleaner()
    p.feed(text.replace("\r\n", "\n"))
    p.close()
    result = p.result()
    # пустые теги оформления (<b></b>) Telegram не нужны
    for _ in range(3):
        result = re.sub(r"<(b|i|u|s|code|pre|tg-spoiler|blockquote)></\1>", "", result)
    return result.strip()


def sms_text(text: str) -> str:
    """Текст SMS от площадки — без HTML-кодов: «&#10;» -> перенос строки, «&amp;#10;» — тоже
    (бывает закодирован дважды; одна и та же SMS из вебхука и из опроса иначе выглядела бы двумя)."""
    for _ in range(3):
        decoded = html.unescape(text or "")
        if decoded == text:
            break
        text = decoded
    return (text or "").replace("\r\n", "\n").strip()


def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))
