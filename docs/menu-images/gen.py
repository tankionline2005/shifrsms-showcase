"""Картинки меню бота в стиле сайта: 1920×1080, HTML -> PNG через Chrome headless.
Запуск: python gen.py (картинки — рядом с этим файлом)."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\path\to\Desktop\Shifr SMS Service")
OUT = Path(__file__).parent
FONTS = (ROOT / "app/web/landing/fonts").as_uri()
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

land = (ROOT / "app/web/landing/partnership.html").read_text(encoding="utf-8")
wall_a = re.search(r'body::before\{inset:[^;]+;background:(url\("data:image/svg\+xml,[^"]+"\))', land).group(1)
wall_b = re.search(r'body::after\{inset:[^;]+;background:(url\("data:image/svg\+xml,[^"]+"\))', land).group(1)
ghost = re.search(r'<symbol id="ghost"[^>]*>(.*?)</symbol>', land, flags=re.S).group(1)

CSS = f"""
@font-face{{font-family:"BitCell";src:url({FONTS}/bitcell.woff) format("woff")}}
@font-face{{font-family:"Plex";font-weight:400;src:url({FONTS}/plex-mono-400.woff) format("woff")}}
@font-face{{font-family:"Plex";font-weight:500;src:url({FONTS}/plex-mono-500.woff) format("woff")}}
:root{{--fg:#f5eef7;--muted:#b3a8b8;--dim:#8a7f8f;--m:#ff00ff;--pink:#ff73ff;--plum:#8b008b;--s1:#0d0a0f;--s2:#16111a;
  --s3:#211a26;--s4:#2f2635;--line:#2b2231;--ok:#5ee6a8;--warn:#ffcf70;--tg:#1b1520;--btn:#231b29;
  --pix:"BitCell",monospace;--mono:"Plex",monospace;--ui:"Segoe UI",-apple-system,Roboto,Arial,sans-serif}}
*{{box-sizing:border-box;margin:0}}
html,body{{width:1920px;height:1080px;overflow:hidden;background:#000}}
body{{position:relative;color:var(--fg);font-family:var(--mono)}}
.wa,.wb{{position:absolute;inset:0;pointer-events:none}}
.wa{{background:{wall_a} 0 0/660px 660px;opacity:1}}
.wb{{background:{wall_b} 210px 120px/450px 450px}}
.glow{{position:absolute;inset:0;background:radial-gradient(46% 70% at 74% 52%,rgba(139,0,139,.42),rgba(60,0,70,.18) 55%,transparent 75%),
  radial-gradient(30% 40% at 8% 100%,rgba(139,0,139,.18),transparent 70%)}}
.vig{{position:absolute;inset:0;background:radial-gradient(120% 120% at 50% 50%,transparent 60%,rgba(0,0,0,.65))}}
.left{{position:absolute;left:96px;top:90px;bottom:90px;width:860px;display:flex;flex-direction:column}}
.logo{{font:96px/1 var(--pix);color:#fff}} .logo b{{font-weight:400;color:var(--m)}}
.claim{{margin-top:auto;margin-bottom:auto;display:grid;gap:52px;padding-bottom:40px}}
.eyebrow{{font:500 22px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--pink)}}
h1{{font:400 192px/.86 var(--pix);color:#fff}} h1 .m{{color:var(--m);display:block;margin-top:-44px;
  text-shadow:0 0 38px rgba(255,0,255,.45)}}
.sub{{font:400 62px/1.3 var(--mono);color:var(--muted);max-width:860px}}
.sub b{{color:var(--fg);font-weight:500}}
.facts{{display:flex;flex-wrap:wrap;gap:12px 34px;font:400 24px/1 var(--mono);color:var(--muted);list-style:none;padding:0}}
.facts li{{display:flex;align-items:center;gap:12px}} .facts li::before{{content:"";width:12px;height:12px;background:var(--m)}}
.handle{{font:500 24px/1 var(--mono);color:var(--dim);letter-spacing:.04em}} .handle b{{color:var(--pink);font-weight:500}}
.right{{position:absolute;right:60px;top:110px;bottom:0;width:990px;display:flex;align-items:center;justify-content:center}}
.stage{{position:relative;transform:perspective(2000px) rotateY(-9deg) rotateX(4deg) scale(1.32);transform-style:preserve-3d}}
.card{{position:relative;background:linear-gradient(160deg,#1a1120 0%,#0d0a0f 60%);border:2px solid #3a2340;border-radius:36px;
  box-shadow:0 50px 90px -30px rgba(0,0,0,.95),0 0 120px -30px rgba(255,0,255,.45);font-family:var(--ui);color:#f2f0f4}}
.ghost{{position:absolute;color:var(--m);filter:drop-shadow(0 0 22px rgba(255,0,255,.55))}}
.ghost svg{{display:block;width:100%;height:auto}}
.lt{{display:grid;place-items:center;border-radius:24%;flex:none;font-style:normal;overflow:hidden}}
.lt svg{{display:block}}
.t3{{position:absolute;transform-style:preserve-3d}}
.t3>i{{position:absolute;inset:0;border-radius:24%}}
.t3f{{display:grid;place-items:center;overflow:hidden;box-shadow:inset 0 0 0 1px rgba(255,255,255,.18)}} .t3f svg{{display:block}}
.t3sh{{position:absolute;border-radius:50%;background:radial-gradient(closest-side,rgba(0,0,0,.75),transparent);filter:blur(10px)}}
.spark{{position:absolute;width:18px;height:18px;background:var(--m);box-shadow:0 0 18px var(--m)}}
"""

GHOST = f'<svg viewBox="0 0 28 29" shape-rendering="crispEdges">{ghost}</svg>'

# ---------------------------------------------------------------- логотипы сервисов (icons/: Simple Icons, CC0; СБП — Wikimedia Commons)
ICONS = Path(__file__).parent / "icons"


def _paths(name: str, fill: str) -> tuple[str, str]:
    """-> (viewBox, <path> с нужной заливкой)."""
    svg = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    vb = re.search(r'viewBox="([^"]+)"', svg).group(1)
    body = re.sub(r"<title>.*?</title>", "", svg.split(">", 1)[1].rsplit("</svg>", 1)[0], flags=re.S)
    if fill:
        body = re.sub(r'\sfill="[^"]*"', "", body).replace("<path", f'<path fill="{fill}"')
    return vb, body


def glyph(name: str, fill: str = "#fff", pad: str = "20%") -> str:
    vb, body = _paths(name, fill)
    return f'<svg viewBox="{vb}" style="width:calc(100% - 2*{pad});height:calc(100% - 2*{pad})">{body}</svg>'


GOOGLE = ('<svg viewBox="0 0 48 48" style="width:60%;height:60%"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 '
          '3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
          '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 '
          '6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59'
          'l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 '
          '11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 '
          '48 24 48z"/></svg>')  # актуальный четырёхцветный «G»
MS = ('<svg viewBox="0 0 23 23" style="width:56%;height:56%"><path fill="#f25022" d="M1 1h10v10H1z"/><path fill="#7fba00" '
      'd="M12 1h10v10H12z"/><path fill="#00a4ef" d="M1 12h10v10H1z"/><path fill="#ffb900" d="M12 12h10v10H12z"/></svg>')
# фон плитки, её «торец» (тёмная грань) и содержимое — как иконки приложений
LOGOS = {
    "Telegram": ("#ffffff", "#9fb4c4", glyph("telegram", "#26a5e4", "14%")),
    "WhatsApp": ("#25d366", "#128c4a", glyph("whatsapp")),
    "Google": ("#ffffff", "#a9b0bb", GOOGLE),
    "Instagram": ("radial-gradient(circle at 30% 107%,#fdf497 0%,#fdf497 5%,#fd5949 45%,#d6249f 60%,#285aeb 90%)", "#8a1e6c",
                  glyph("instagram")),
    "Discord": ("#5865f2", "#2f37a5", glyph("discord")),
    "OpenAI": ("#202123", "#4a4a52", glyph("openai", "#fff", "20%")),
    "Tinder": ("linear-gradient(135deg,#ff6036,#fd267a)", "#a3123f", glyph("tinder")),
    "Microsoft": ("#ffffff", "#a9b0bb", MS),
}


def logo_tile(name: str, size: int, extra: str = "", depth: int = 0) -> str:
    """Плитка-иконка сервиса. depth — толщина «торца» в пикселях (3D), 0 — плоская."""
    bg, edge, inner = LOGOS[name]
    shadow = ",".join(f"{i}px {i}px 0 {edge}" for i in range(1, depth + 1))
    shadow = (shadow + "," if shadow else "") + "0 24px 50px -14px rgba(0,0,0,.85)"
    return (f'<i class="lt" style="width:{size}px;height:{size}px;background:{bg};box-shadow:{shadow};{extra}">'
            f'{inner}</i>')


def logo_tile3d(name: str, size: int, x: int, y: int, rx: float, ry: float, rz: float) -> str:
    """Объёмная плитка: лицо и слои «торца» за ним по оси Z — при наклоне грань видна с той стороны, с какой надо."""
    bg, edge, inner = LOGOS[name]
    depth = max(12, size // 9)
    layers = "".join(f'<i style="transform:translateZ(-{i}px);background:{edge};filter:brightness({1 - i / depth * .35:.2f})"></i>'
                     for i in range(depth, 0, -1))
    return (f'<div class="t3sh" style="left:{x + size * .08:.0f}px;top:{y + size * .72:.0f}px;width:{size * .9:.0f}px;'
            f'height:{size * .45:.0f}px"></div>'
            f'<div class="t3" style="left:{x}px;top:{y}px;width:{size}px;height:{size}px;transform:perspective(900px) '
            f'rotateX({rx}deg) rotateY({ry}deg) rotateZ({rz}deg)">{layers}<i class="t3f" style="background:{bg}">{inner}</i></div>')


def page(eyebrow, title1, title2, sub, facts, right, extra_css=""):
    facts_html = "".join(f"<li>{f}</li>" for f in facts)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>{CSS}{extra_css}</style></head><body>
<div class="wa"></div><div class="wb"></div><div class="glow"></div><div class="vig"></div>
<div class="left">
  <div class="logo">Shifr<b>SMS</b></div>
  <div class="claim">
    <h1>{title1}<span class="m">{title2}</span></h1>
    <p class="sub">{sub}</p>
  </div>
</div>
<div class="right">{right}</div>
</body></html>"""


# ---------------------------------------------------------------- 1. Получить СМС — иллюстрация: призрак, сервисы вокруг, пришёл код
TILES = [  # сервис, x, y, размер, наклон X, наклон Y, поворот
    ("Telegram", 50, 250, 160, 18, -22, -8), ("WhatsApp", 790, 350, 136, 14, 24, 8), ("Google", 50, 600, 132, -16, -20, 7),
    ("Instagram", 760, 630, 140, -14, 22, -7), ("Discord", 280, 800, 108, -20, -10, -6), ("OpenAI", 560, 810, 112, -18, 16, 9),
    ("Microsoft", 120, 100, 104, 16, -16, 6)]
tiles = "".join(logo_tile3d(n, w, x, y, rx, ry, rz) for n, x, y, w, rx, ry, rz in TILES)
buy = page("Получить SMS", "Популярные", "сервисы",
           "Код из SMS придёт<br><b>прямо в бот</b>",
           [],
           f"""<div class="hero">{tiles}
  <div class="big-ghost">{GHOST}</div>
  <div class="sms"><span class="ic">📩</span><div><small>Telegram · SMS-код</small><b>51824</b></div></div>
  <div class="spark" style="left:150px;top:470px"></div><div class="spark" style="left:880px;top:300px;width:14px;height:14px"></div>
  <div class="spark" style="left:440px;top:760px;width:12px;height:12px"></div>
</div>""",
           """.right{top:0}
.hero{position:relative;width:940px;height:900px}
.big-ghost{position:absolute;left:240px;top:330px;width:440px;color:var(--m);filter:drop-shadow(0 0 40px rgba(255,0,255,.65))}
.big-ghost svg{display:block;width:100%;height:auto}
.sms{position:absolute;left:330px;top:70px;display:grid;grid-template-columns:96px auto;gap:24px;align-items:center;
  padding:26px 44px 26px 26px;border-radius:36px 36px 36px 12px;background:rgba(32,24,38,.96);border:2px solid var(--m);
  box-shadow:0 30px 60px -20px rgba(0,0,0,.9),0 0 70px -10px rgba(255,0,255,.55);font-family:var(--ui)}
.sms .ic{width:96px;height:96px;border-radius:24px;background:#2aa3e0;display:grid;place-items:center;font-size:52px}
.sms small{display:block;font-size:36px;color:#cfc7d4} .sms b{display:block;font:500 88px/1.05 var(--mono);color:#fff;letter-spacing:.06em}""")

# ---------------------------------------------------------------- 2. Баланс — способы пополнения
land_usdt = re.search(r'<symbol id="usdt"[^>]*>(.*?)</symbol>', land, flags=re.S).group(1)
BTC = (f'<span class="coin"><i class="disc"></i>{glyph("bitcoin", "#f7931a", "0%")}</span>')
ETH = f'<span class="coin" style="background:#627eea">{glyph("ethereum", "#fff", "20%")}</span>'
USDT = f'<span class="coin"><svg viewBox="0 0 32 32" style="width:100%;height:100%">{land_usdt}</svg></span>'


SBP = f'<i class="ic sbp">{glyph("sbp", "", "16%")}</i>'
CRYPTO = (f'<i class="ic coins">{BTC}{ETH}{USDT}'
          '</i>')
STARS_ = f'<i class="ic star"><img src="{(ICONS / "stars.svg").as_uri()}"></i>'
methods = "".join(f'<div class="mt">{ic}<b>{t}</b></div>' for ic, t in
                  ((SBP, "СБП"), (CRYPTO, "Криптовалюта"), (STARS_, "Telegram Stars")))
balance = page("Баланс", "Ваш", "баланс",
               "СБП, криптовалюта<br><b>или звёзды Telegram</b>",
               [],
               f"""<div class="stage"><div class="card bal">
  <div class="hd">Способы пополнения</div>
  {methods}
</div>
<div class="push"><i>✓</i><div><div class="ph"><span>ShifrSMS</span><span>сейчас</span></div><div class="pt">Платёж <b>зачислен</b> на баланс</div></div></div>
<div class="ghost" style="width:140px;top:-112px;left:30px">{GHOST}</div>
<div class="spark" style="top:-30px;right:60px"></div></div>""",
               """.stage{transform:perspective(2000px) rotateY(-9deg) rotateX(4deg) scale(1.42)}
.bal{width:600px;padding:32px 30px 30px;display:grid;gap:14px}
.hd{font-size:30px;font-weight:600;color:#fff;margin-bottom:4px}
.right{top:80px}
.mt{display:grid;grid-template-columns:150px 1fr;gap:22px;align-items:center;background:var(--btn);border-radius:20px;padding:18px 22px}
.mt b{font-size:36px;font-weight:700}
.ic{height:80px;display:flex;align-items:center;justify-self:center;font-style:normal}
.ic.sbp{width:80px;justify-content:center;background:#fff;border-radius:18px} .ic.sbp svg{width:62%!important;height:76%!important}
.coin{position:relative;width:64px;height:64px;border-radius:50%;display:grid;place-items:center;overflow:hidden;box-shadow:0 0 0 4px var(--btn)}
.coin+.coin{margin-left:-20px} .coin svg{display:block;position:relative}
.coin .disc{position:absolute;inset:12%;border-radius:50%;background:#fff}  /* под прорезью «B» — белый, без белого ободка */
.ic.star{width:92px;justify-content:center} .ic.star img{width:92px;height:92px;filter:drop-shadow(0 6px 16px rgba(255,190,0,.4))}
.push{position:absolute;right:-40px;bottom:-62px;width:470px;display:grid;grid-template-columns:54px 1fr;gap:16px;align-items:center;
  padding:18px 22px;border-radius:24px;background:rgba(40,32,46,.94);border:1px solid #3a2f42;box-shadow:0 24px 50px rgba(0,0,0,.6);font-family:var(--ui)}
.push i{width:54px;height:54px;border-radius:14px;background:var(--ok);color:#06331f;display:grid;place-items:center;font:700 30px/1 var(--ui);font-style:normal}
.ph{display:flex;justify-content:space-between;font-size:20px;color:#b9b0bf} .pt{font-size:26px;font-weight:600;margin-top:4px} .pt b{color:var(--ok)}""")

# ---------------------------------------------------------------- 3. Мои заказы
ROWS = [("Telegram", "Индонезия", "#2aa3e0", "✅ SMS получена", "ok", "код <b>51824</b>"),
        ("WhatsApp", "Колумбия", "#25d366", "⏳ Ожидаем SMS", "wait", "активна до 18:42"),
        ("Google", "Индия", "#ea4335", "☑️ Завершён", "done", "код <b>730 412</b>"),
        ("Discord", "Казахстан", "#5865f2", "❌ Отменён", "off", "деньги на балансе")]
rows = "".join(f'<div class="row">{logo_tile(n, 64, "border-radius:22%")}<div class="t"><b>{n}</b><span>{cn}</span></div>'
               f'<div class="st {cls}"><span>{st}</span><small>{note}</small></div></div>' for n, cn, c, st, cls, note in ROWS)
orders = page("Мои заказы", "Ваши", "заказы",
              "Все активации и коды<br><b>в одном месте</b>",
              ["Коды из SMS", "Статус каждой активации", "Повторить в одно нажатие"],
              f"""<div class="stage"><div class="card ord">{rows}</div>
<div class="ghost" style="width:140px;top:-112px;right:40px">{GHOST}</div>
<div class="spark" style="bottom:-34px;left:60px"></div></div>""",
              """.ord{width:740px;padding:14px 26px}
.row{display:grid;grid-template-columns:64px 1fr auto;gap:18px;align-items:center;padding:20px 0}
.row+.row{border-top:1px solid var(--line)}

.t{display:grid;gap:4px} .t b{font-size:33px;font-weight:600} .t span{font-size:25px;color:#a99fae}
.st{display:grid;gap:6px;justify-items:end;text-align:right} .st span{font-size:26px;font-weight:600;padding:7px 16px;border-radius:999px}
.st small{font-size:24px;color:#cfc7d4} .st small b{font-family:var(--mono);color:#fff;font-weight:500}
.st.ok span{background:rgba(94,230,168,.15);color:var(--ok)} .st.wait span{background:rgba(255,207,112,.14);color:var(--warn)}
.st.done span{background:rgba(255,0,255,.14);color:var(--pink)} .st.off span{background:rgba(255,255,255,.08);color:#b9b0bf}""")

# ---------------------------------------------------------------- 4. Информация — как это работает
STEPS = [("1", "Выберите сервис", "и страну номера"), ("2", "Укажите номер", "в нужном сервисе"),
         ("3", "Получите код", "он придёт прямо в бот")]
steps = "".join(f'<div class="stp"><b>{n}</b><div><span>{a}</span><small>{c}</small></div></div>' for n, a, c in STEPS)
info = page("Информация", "Как это", "работает?",
            "Одна активация —<br><b>один код</b>",
            ["~20 минут на активацию", "Поддержка в боте", "Мы не храним ваши аккаунты"],
            f"""<div class="stage"><div class="card inf">{steps}
  <div class="ret">↩ Не пришла SMS — возврат на баланс</div></div>
<div class="ghost" style="width:150px;top:-118px;right:30px">{GHOST}</div>
<div class="spark" style="top:-26px;left:70px"></div></div>""",
            """.inf{width:680px;padding:28px 30px 30px;display:grid;gap:14px}
.stp{display:grid;grid-template-columns:88px 1fr;gap:22px;align-items:center;background:var(--btn);border-radius:20px;padding:18px 22px}
.stp b{font:400 80px/1 var(--pix);color:var(--m);text-align:center;text-shadow:0 0 24px rgba(255,0,255,.5)}
.stp span{display:block;font-size:36px;font-weight:600} .stp small{display:block;font-size:27px;color:#a99fae;margin-top:4px}
.ret{margin-top:6px;border:2px dashed #4a3352;border-radius:18px;padding:16px 20px;font-size:27px;color:var(--ok);text-align:center}""")

# ---------------------------------------------------------------- 5. Пригласить друга
invite = page("Пригласить друга", "Пригласите", "друзей",
              "<b>15%</b> с активаций<br>друга — <b>навсегда</b>",
              ["Своя ссылка", "Начисления сразу", "Без лимитов"],
              f"""<div class="stage"><div class="card inv">
  <div class="big">+15<span>%</span></div>
  <div class="cap">с каждой активации друга</div>
  <div class="lnk"><span>shifrsms.shop/r/k7f3x9qa</span><em>копировать</em></div>
  <div class="stats"><div><b>12</b><small>приглашено</small></div><div><b>184 ₽</b><small>заработано</small></div></div>
</div>
<div class="push"><i>🎉</i><div><div class="ph"><span>ShifrSMS</span><span>сейчас</span></div><div class="pt"><b>+15 ₽</b>: ваш друг получил SMS</div></div></div>
<div class="ghost" style="width:140px;top:-104px;left:26px">{GHOST}</div>
<div class="spark" style="top:-34px;right:80px"></div></div>""",
              """.right{top:80px}
.inv{width:660px;padding:36px 34px 32px;display:grid;gap:16px}
.big{font:400 160px/0.9 var(--pix);color:var(--m);text-shadow:0 0 40px rgba(255,0,255,.55)} .big span{font-size:96px}
.cap{font-size:31px;color:#cfc7d4;margin-top:-4px}
.lnk{display:flex;justify-content:space-between;align-items:center;gap:14px;border:2px dashed var(--m);background:rgba(255,0,255,.06);
  padding:18px 20px;font:500 21px/1 var(--mono);color:#fff;white-space:nowrap} .lnk em{font-style:normal;font-size:16px;letter-spacing:.1em;text-transform:uppercase;color:var(--pink)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.stats div{background:var(--btn);border-radius:16px;padding:16px 20px} .stats b{display:block;font-size:42px;font-weight:700}
.stats small{font-size:25px;color:#a99fae}
.push{position:absolute;right:-44px;bottom:-70px;width:540px;display:grid;grid-template-columns:54px 1fr;gap:16px;align-items:center;
  padding:18px 22px;border-radius:24px;background:rgba(40,32,46,.95);border:1px solid #3a2f42;box-shadow:0 24px 50px rgba(0,0,0,.6);font-family:var(--ui)}
.push i{width:54px;height:54px;border-radius:14px;background:#3a1640;box-shadow:inset 0 0 0 2px #6b2a73;display:grid;place-items:center;font-size:30px;font-style:normal}
.ph{display:flex;justify-content:space-between;font-size:21px;color:#b9b0bf} .pt{font-size:28px;font-weight:600;margin-top:4px} .pt b{color:var(--ok)}""")

# ---------------------------------------------------------------- 6. Веб-версия — окно браузера: вход через Telegram или анонимно
TG_PLANE = glyph("telegram", "#fff", "0%")
web = page("Веб-версия", "Веб-", "версия",
           "Тот же бот —<br><b>в браузере</b>",
           [],
           f"""<div class="stage"><div class="card br">
  <div class="bar"><i></i><i></i><i></i><div class="url"><span class="lock">🔒</span>shifrsms.shop<em>/shop</em></div></div>
  <div class="bd">
    <div class="tgb"><span class="pl">{TG_PLANE}</span>Войти через Telegram</div>
    <div class="hint2">баланс и заказы — общие с ботом</div>
    <div class="or"><span>или анонимно</span></div>
    <div class="fld"><small>Логин</small><b>ghost_42</b></div>
    <div class="fld"><small>Пароль</small><b>••••••••••<i class="cur"></i></b></div>
  </div>
</div>
<div class="ghost" style="width:140px;top:-108px;right:40px">{GHOST}</div>
<div class="spark" style="bottom:-34px;left:70px"></div></div>""",
           """.right{top:80px}
.stage{transform:perspective(2000px) rotateY(-9deg) rotateX(4deg) scale(1.36)}
.br{width:700px;overflow:hidden}
.bar{display:flex;align-items:center;gap:10px;padding:18px 22px;background:#1d1523;border-bottom:1px solid var(--line)}
.bar>i{width:16px;height:16px;border-radius:50%;background:#4a3d52} .bar>i:first-child{background:var(--m)}
.url{flex:1;margin-left:14px;background:#0f0b12;border-radius:999px;padding:10px 20px;font:500 22px/1 var(--mono);color:#fff}
.url em{font-style:normal;color:var(--dim)} .lock{font-size:18px;margin-right:10px}
.bd{padding:32px 34px 34px;display:grid;gap:14px}
.tgb{display:flex;align-items:center;justify-content:center;gap:16px;background:#2aa3e0;border-radius:18px;padding:22px;
  font-size:32px;font-weight:700;color:#fff;box-shadow:0 12px 30px -10px rgba(42,163,224,.6)}
.pl{width:38px;height:38px;display:grid;place-items:center} .pl svg{width:38px!important;height:38px!important}
.hint2{text-align:center;font-size:23px;color:var(--ok)}
.or{display:flex;align-items:center;gap:18px;margin:10px 0 2px;font-size:23px;color:#a99fae}
.or::before,.or::after{content:"";flex:1;height:1px;background:var(--line)}
.fld{background:var(--btn);border-radius:16px;padding:14px 22px;display:grid;gap:4px}
.fld small{font-size:20px;color:#a99fae} .fld b{font:500 30px/1.2 var(--mono);color:#fff;display:flex;align-items:center}
.cur{display:inline-block;width:3px;height:30px;background:var(--m);margin-left:6px}""")

# ---------------------------------------------------------------- 7. Профиль — баланс и три раздела
PROFILE_ROWS = [("💰", "Баланс", "пополнение: СБП, крипта, звёзды"), ("📋", "Мои заказы", "активации и коды"),
                ("🤝", "Пригласить друга", "15% с активаций друга")]
prows = "".join(f'<div class="pr"><i>{e}</i><div><b>{t}</b><small>{s}</small></div><em>›</em></div>'
                for e, t, s in PROFILE_ROWS)
profile = page("Профиль", "Ваш", "профиль",
               "Баланс, заказы<br><b>и приглашения</b>",
               [],
               f"""<div class="stage"><div class="card prf">
  <div class="who"><span class="av">{GHOST}</span><div><small>Ваш баланс</small><b class="mask"><span>••••</span> ₽<i class="eye"></i></b></div></div>
  {prows}
</div>
<div class="ghost" style="width:130px;top:-100px;right:46px">{GHOST}</div>
<div class="spark" style="top:-30px;left:80px"></div><div class="spark" style="bottom:-30px;right:120px;width:12px;height:12px"></div></div>""",
               """.right{top:80px}
.stage{transform:perspective(2000px) rotateY(-9deg) rotateX(4deg) scale(1.38)}
.prf{width:660px;padding:30px 30px 30px;display:grid;gap:14px}
.who{display:grid;grid-template-columns:96px 1fr;gap:24px;align-items:center;padding:4px 6px 18px;border-bottom:1px solid var(--line);margin-bottom:4px}
.av{width:96px;height:96px;border-radius:50%;background:#2a1530;box-shadow:inset 0 0 0 2px #6b2a73;display:grid;place-items:center;color:var(--m)}
.av svg{width:58px;height:auto;display:block}
.who small{display:block;font-size:24px;color:#a99fae} .who b{display:flex;align-items:center;gap:14px;font:400 76px/1 var(--pix);color:#fff;margin-top:6px}
.mask span{letter-spacing:.08em;color:var(--pink);text-shadow:0 0 22px rgba(255,0,255,.5)}
.eye{position:relative;width:34px;height:21px;margin-left:8px;border:3px solid #4a3f52;border-radius:50% / 60%}
.eye::before{content:"";position:absolute;left:50%;top:50%;width:9px;height:9px;margin:-4.5px 0 0 -4.5px;border-radius:50%;background:#4a3f52}
.eye::after{content:"";position:absolute;left:-6px;top:50%;width:46px;height:3px;margin-top:-1.5px;background:#4a3f52;transform:rotate(-35deg)}
.pr{display:grid;grid-template-columns:64px 1fr 24px;gap:20px;align-items:center;background:var(--btn);border-radius:20px;padding:18px 22px}
.pr i{width:64px;height:64px;border-radius:16px;background:#2a1530;display:grid;place-items:center;font:44px/1 "Segoe UI Emoji","Apple Color Emoji",sans-serif;font-style:normal}
.pr b{display:block;font-size:33px;font-weight:600} .pr small{display:block;font-size:23px;color:#a99fae;margin-top:3px}
.pr em{font-style:normal;font-size:44px;color:var(--pink);line-height:1}""")


def finish(png: Path) -> None:
    """Постобработка: немного резкости, затем зерно (одинаковое для всех каналов — без цветных точек)."""
    import numpy as np
    from PIL import Image as Img, ImageFilter
    im = Img.open(png).convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.4, percent=70, threshold=2))
    a = np.asarray(im).astype(np.float32)
    rng = np.random.default_rng(7)
    a += rng.normal(0, 7, a.shape[:2])[..., None]
    Img.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(png, optimize=True)


ONLY = sys.argv[1:]  # python gen.py web profile — только эти картинки
for key, html in {"buy": buy, "web": web, "info": info, "profile": profile, "balance": balance, "orders": orders,
                  "invite": invite}.items():
    if ONLY and key not in ONLY:
        continue
    f = OUT / f"{key}.html"
    f.write_text(html, encoding="utf-8")
    png = OUT / f"{key}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--allow-file-access-from-files", "--virtual-time-budget=3000",
                    f"--window-size=1920,1080", f"--screenshot={png}", f.as_uri()],
                   check=True, capture_output=True, timeout=120)
    finish(png)
    print(key, png.stat().st_size)

from PIL import Image
KEYS = ("buy", "web", "info", "profile", "balance", "orders", "invite")
sheet = Image.new("RGB", (1920, 1080 * len(KEYS) + 40 * (len(KEYS) - 1)), "white")
for i, k in enumerate(KEYS):
    sheet.paste(Image.open(OUT / f"{k}.png").convert("RGB"), (0, i * (1080 + 40)))
sheet.resize((960, sheet.height // 2)).save(OUT / "sheet.png")
