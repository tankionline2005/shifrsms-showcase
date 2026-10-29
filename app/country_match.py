"""Сопоставление произвольного списка названий стран (вставленного из буфера) со странами каталога.

В разных источниках одна страна пишется по-разному: «Восточное Самоа» / «Американское Самоа»,
«Кот-д'Ивуар» / «Берег Слоновой Кости», «Южная Корея» / «Корея, Республика», «ОАЭ».
Поэтому ищем по нескольким источникам сразу: наши названия, официальные русские и английские
(pycountry), английские названия площадок, ISO-коды и словарь разговорных вариантов.
"""

import difflib
import gettext
import re
from dataclasses import dataclass

import pycountry

_ru = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["ru"], fallback=True)

# Разговорные и устаревшие варианты -> ISO2
ALIASES = {
    "восточноесамоа": "AS", "американскоесамоа": "AS", "западноесамоа": "WS",
    "сша": "US", "америка": "US", "соединенныештатыамерики": "US", "штаты": "US",
    "англия": "GB", "британия": "GB", "великобритания": "GB", "соединенноекоролевство": "GB",
    "оаэ": "AE", "эмираты": "AE", "арабскиеэмираты": "AE",
    "южнаякорея": "KR", "корея": "KR", "республикакорея": "KR", "кндр": "KP",
    "севернаякорея": "KP", "россия": "RU", "рф": "RU", "беларусь": "BY", "белоруссия": "BY",
    "молдавия": "MD", "молдова": "MD", "киргизия": "KG", "кыргызстан": "KG",
    "чехия": "CZ", "чешскаяреспублика": "CZ", "словакия": "SK",
    "котдивуар": "CI", "берегслоновойкости": "CI", "конгодрк": "CD", "дрконго": "CD",
    "демократическаяреспубликаконго": "CD", "конгокиншаса": "CD", "заир": "CD",
    "конго": "CG", "республикаконго": "CG", "конгобраззавиль": "CG",
    "бирма": "MM", "мьянма": "MM", "лаос": "LA", "вьетнам": "VN", "иран": "IR",
    "сирия": "SY", "палестина": "PS", "танзания": "TZ", "боливия": "BO", "венесуэла": "VE",
    "тайвань": "TW", "макао": "MO", "аомынь": "MO", "гонконг": "HK", "сянган": "HK",
    "македония": "MK", "севернаямакедония": "MK", "косово": "XK", "ватикан": "VA",
    "свазиленд": "SZ", "эсватини": "SZ", "восточныйтимор": "TL", "тиморлесте": "TL",
    "кабоверде": "CV", "островазеленогомыса": "CV", "зеленыймыс": "CV",
    "брунеи": "BN", "бруней": "BN", "микронезия": "FM", "фарерскиеострова": "FO",
    "фареры": "FO", "реюньон": "RE", "синтмартен": "SX", "сенмартен": "MF",
    "сенткитсиневис": "KN", "сентлюсия": "LC", "сентвинсентигренадины": "VC",
    "сентвинсент": "VC", "тринидад": "TT", "тринидадитобаго": "TT", "антигуа": "AG",
    "антигуаибарбуда": "AG", "босния": "BA", "боснияигерцеговина": "BA",
    "папуановаягвинея": "PG", "новаягвинея": "PG", "цар": "CF",
    "центральноафриканскаяреспублика": "CF", "экваториальнаягвинея": "GQ",
    "гвинеябисау": "GW", "сантомеипринсипи": "ST", "доминикана": "DO",
    "доминиканскаяреспублика": "DO", "доминика": "DM", "гаити": "HT", "ямайка": "JM",
    "каймановыострова": "KY", "виргинскиеострова": "VG", "британскиевиргинскиеострова": "VG",
    "американскиевиргинскиеострова": "VI", "нидерланды": "NL", "голландия": "NL",
    "турция": "TR", "турецкаяреспублика": "TR", "египет": "EG", "ливия": "LY",
    "казахстан": "KZ", "узбекистан": "UZ", "таджикистан": "TJ", "туркмения": "TM",
    "туркменистан": "TM", "грузия": "GE", "армения": "AM", "азербайджан": "AZ",
    "германия": "DE", "фрг": "DE", "израиль": "IL", "юар": "ZA", "южнаяафрика": "ZA",
    "южныйсудан": "SS", "судан": "SD", "сомали": "SO", "гвиана": "GF",
    "французскаягвиана": "GF", "мартиника": "MQ", "гваделупа": "GP",
    "новаякаледония": "NC", "французскаяполинезия": "PF", "пуэрторико": "PR",
    "гренландия": "GL", "бермуды": "BM", "бермудскиеострова": "BM", "ангилья": "AI",
    "аруба": "AW", "кюрасао": "CW", "монтсеррат": "MS", "теркскиеикайкос": "TC",
    "аландскиеострова": "AX", "сальвадор": "SV", "эльсальвадор": "SV",
    "намибия": "NA", "лихтенштейн": "LI", "монако": "MC", "санмарино": "SM",
}

VIRTUAL_WORDS = ("виртуал", "virtual")


@dataclass
class CountryRef:
    id: int
    key: str
    name: str
    provider_names: list[str]


@dataclass
class Match:
    line: str
    country: CountryRef
    how: str  # exact | fuzzy


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"^\s*[\d\W_]+", "", s)  # «1. Страна», «- Страна», эмодзи-флаги
    return re.sub(r"[^a-zа-я0-9]", "", s)


def _iso_names(iso: str) -> list[str]:
    c = pycountry.countries.get(alpha_2=iso)
    if not c:
        return []
    names = [c.name, getattr(c, "official_name", ""), getattr(c, "common_name", "")]
    names += [_ru.gettext(n) for n in list(names) if n]
    return [n for n in names if n]


def build_index(countries: list[CountryRef]) -> tuple[dict[str, CountryRef], dict[str, list[CountryRef]]]:
    """-> (нормализованное название -> страна, ISO2 -> страны с этим кодом)."""
    index: dict[str, CountryRef] = {}
    by_iso: dict[str, list[CountryRef]] = {}
    base_first = sorted(countries, key=lambda c: "_" in c.key)  # основные страны раньше вариантов
    for c in base_first:
        iso = c.key[:2]
        by_iso.setdefault(iso, []).append(c)
        names = [c.name, *c.provider_names]
        if "_" not in c.key:
            names += _iso_names(iso) + [iso]
        for n in names:
            k = _norm(n)
            if k and k not in index:
                index[k] = c
    return index, by_iso


def _iso_of(k: str) -> str | None:
    """Распознать страну вообще (даже если её нет в нашем каталоге) — по ISO-справочнику."""
    if k in ALIASES:
        return ALIASES[k]
    return _ISO_BY_NAME.get(k)


def _iso_by_name() -> dict[str, str]:
    result = {}
    for c in pycountry.countries:
        for n in _iso_names(c.alpha_2):
            result.setdefault(_norm(n), c.alpha_2)
    return result


_ISO_BY_NAME = _iso_by_name()


def match(text: str, countries: list[CountryRef]) -> tuple[list[Match], list[str], list[str]]:
    """Разбирает вставленный текст (по строке на страну; допускаются запятые и «;»).
    -> (найденные, нераспознанные строки, распознанные страны, которых нет в каталоге)."""
    index, by_iso = build_index(countries)
    base = {iso: next((c for c in cs if "_" not in c.key), cs[0]) for iso, cs in by_iso.items()}
    keys = list(index)
    matches: list[Match] = []
    unknown: list[str] = []
    absent: list[str] = []
    seen: set[int] = set()

    def resolve(line: str):
        """-> Match | "absent" | None (не распознано)."""
        k = _norm(line)
        if not k:
            return "skip"
        wants_virtual = any(w in line.lower() for w in VIRTUAL_WORDS)
        found, how = None, "exact"
        # «Мьянма (Бирма)» — пробуем и целиком, и без скобок
        variants = [k]
        bare = _norm(re.sub(r"\(.*?\)", "", line))
        if bare and bare != k and not wants_virtual:
            variants.append(bare)
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 2 and all(parts):  # «Корея, Республика» -> «Республика Корея»
            variants.append(_norm(f"{parts[1]} {parts[0]}"))
        for v in variants:
            found = index.get(v) or (base.get(ALIASES[v]) if v in ALIASES else None)
            if found:
                break
        if not found:
            iso = next((i for v in variants if (i := _iso_of(v))), None)
            if iso and iso not in base:
                return "absent"  # страна настоящая, но номеров её у площадок нет
            if iso:
                found = base[iso]
        if not found:
            close = difflib.get_close_matches(k, keys, n=1, cutoff=0.82)
            if close:
                found, how = index[close[0]], "fuzzy"
        if not found:
            return None
        if wants_virtual:  # «США (виртуальные)» — берём вариант страны, если он есть
            virt = next((c for c in by_iso.get(found.key[:2], []) if c.key.endswith("_V")), None)
            found = virt or found
        return Match(line, found, how)

    def add(line: str, result) -> None:
        if result == "skip":
            return
        if result == "absent":
            absent.append(line)
        elif result is None:
            unknown.append(line)
        elif result.country.id not in seen:
            seen.add(result.country.id)
            matches.append(result)

    for raw in re.split(r"[\n;]+", text):
        line = raw.strip()
        if not line:
            continue
        result = resolve(line)
        # запятая бывает внутри названия («Бонайре, Синт-Эстатиус и Саба») — делим строку
        # на части, только если целиком она не распознана
        if (result is None or (isinstance(result, Match) and result.how == "fuzzy")) and "," in line:
            parts = [x.strip() for x in line.split(",") if x.strip()]
            # у кусков — только точные совпадения: «Республика» не должна стать Чадом
            results = [r if not (isinstance(r, Match) and r.how == "fuzzy") else None
                       for r in (resolve(x) for x in parts)]
            if any(isinstance(r, Match) and r.how == "exact" or r == "absent" for r in results):
                for part, r in zip(parts, results):
                    add(part, r)
                continue
        add(line, result)
    return matches, unknown, absent
