"""Английские названия стран для сайта (catalog хранит русские): по ключу страны (ISO2 или ISO2_<вариант>)."""

import re
from functools import lru_cache

import pycountry

SHORT = {"RU": "Russia", "KR": "South Korea", "KP": "North Korea", "IR": "Iran", "VN": "Vietnam", "TW": "Taiwan",
         "BO": "Bolivia", "VE": "Venezuela", "TZ": "Tanzania", "MD": "Moldova", "LA": "Laos", "SY": "Syria",
         "CD": "DR Congo", "CG": "Congo", "GB": "United Kingdom", "US": "USA", "XK": "Kosovo", "PS": "Palestine",
         "MK": "North Macedonia", "BN": "Brunei", "CZ": "Czechia", "TR": "Türkiye", "FM": "Micronesia"}


@lru_cache(maxsize=1024)
def english(key: str, name_ru: str = "") -> str:
    iso = (key or "")[:2].upper()
    base = SHORT.get(iso)
    if not base:
        c = pycountry.countries.get(alpha_2=iso)
        base = (getattr(c, "common_name", None) or c.name) if c else (name_ru or key)
    if key.endswith("_V"):
        return f"{base} (virtual)"
    m = re.search(r"\(([^)]*)\)\s*$", name_ru or "")
    if "_" in key and m and not re.search("[а-яА-Я]", m.group(1)):
        return f"{base} ({m.group(1)})"
    return base
