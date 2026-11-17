"""Conservative comparison of supplied identity details; no fuzzy house matching."""
from __future__ import annotations

import re


def phone_numbers(value):
    numbers = set()
    for part in re.split(r"[;,\n|/]|\bили\b", str(value or ""), flags=re.I):
        part = re.split(r"\b(?:доб\.?|добавочный|ext\.?)\s*:?", part, maxsplit=1, flags=re.I)[0]
        digits = re.sub(r"\D", "", part)
        if len(digits) == 10:
            numbers.add("7" + digits)
        elif len(digits) == 11 and digits[0] in "78":
            numbers.add("7" + digits[1:])
    return numbers


def address_key(value):
    value = str(value or "").casefold().replace("ё", "е")
    # A municipal district marker often precedes a duplicate city component.
    value = re.sub(r"\bг\s*\.\s*о\s*\.\s*", "", value)
    value = re.sub(r"\bгородской\s+округ\b", "", value)
    aliases = {"обл": "область", "г": "город", "ул": "улица", "д": "дом",
               "корп": "корпус", "к": "корпус", "стр": "строение", "оф": "офис",
               "пом": "помещение", "просп": "проспект", "пр-т": "проспект",
               "пер": "переулок", "ш": "шоссе"}
    components = []
    for part in re.split(r"[,;\n]+", value):
        tokens = re.findall(r"[а-яa-z0-9]+(?:[/\-][а-яa-z0-9]+)*", part)
        component = " ".join(aliases.get(token, token) for token in tokens)
        if component and (not components or component != components[-1]):
            components.append(component)
    return " ".join(components)


def detail_key(value, field):
    value = str(value or "").casefold().replace("ё", "е")
    if field == "phone": return " ".join(sorted(phone_numbers(value)))
    if field == "legal_address": return address_key(value)
    if field == "ogrn": return re.sub(r"\D", "", value)
    tokens = re.findall(r"\w+", value)
    return " ".join(sorted(tokens) if field == "legal_name" else tokens)


def details_match(found, supplied, field):
    if field == "phone":
        return bool(phone_numbers(found) & phone_numbers(supplied))
    key = detail_key(found, field)
    return bool(key and key == detail_key(supplied, field))
