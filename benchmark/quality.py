"""Keep benign collection notes separate from mandatory review reasons.

Only recognised informational messages are downgraded. Unknown wording and
ambiguous entity/address claims remain reasons for review.
"""
from __future__ import annotations

import re

RESEARCH_REVIEW_REASONS = {
    "site_unavailable": "Сайт недоступен для исследования.",
    "not_found": "Официальный сайт не найден в пределах проведённого поиска.",
    "ambiguous": "Результат исследования неоднозначен.",
    "needs_review": "Исследование требует дополнительных уточнений.",
}


def informational(message, card, company):
    text = " ".join(message.casefold().replace("ё", "е").split())
    confirmed = card["identity"]["status"].startswith("confirmed")
    if not confirmed:
        return False
    if text.rstrip(".") == ("контактный адрес сайта отличается от юридического адреса; назначение адресов различается, конфликт реквизитов не установлен"):
        return True
    if re.fullmatch(r"адрес [\w.+-]+@[\w.-]+\.[a-z]{2,} из входной карточки не подтвержден открытыми страницами\.?", text):
        mentioned = set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", text))
        supplied = set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", str(company.get("email", "")).casefold()))
        accepted = str(card["facts"]["email"]["value"] or "").casefold()
        return bool(mentioned and mentioned <= supplied and not any(email in accepted for email in mentioned))
    # These messages can change the meaning or ownership of accepted facts.
    if any(term in text for term in ["тождество", "исключительная принадлежность", "расход", "расхожд",
            "конфликт", "групп", "другому юридическому", "другого юридического", "не подтверждены отдельно",
            "исключены источники", "исключены сведения", "требует проверки", "требуется проверка",
            "не подтвержден", "не установле", "не совпад", "противореч", "неверн", "сомнен"]):
        return False
    if ("не открыл" in text or "не открылись" in text) and "сниппет" in text and "не использован" in text:
        return True
    if "уточненная ссылка" in text and "недоступна" in text and "открытие найденного сайта" in text and "успешно" in text:
        return True
    if "исходный адрес" in text and "версия" in text and "доступны" in text:
        return True
    if "ошибке компонента" in text and "однако" in text and "совпадающий инн" in text:
        return card["identity"]["status"] == "confirmed_by_inn"
    if ("вторичн" in text and ("государственн" in text or "егрюл" in text)
            and ("не выполнен" in text or "не открывал" in text or "не проверен" in text)):
        return True
    if (card["identity"]["status"] == "confirmed_by_details"
            and "инн и полное юридическое название" in text and "не обнаружены" in text
            and "принадлежность подтверждена адресом и телефоном" in text):
        return True
    return False


def classify_warnings(warnings, card, company):
    review, notes = [], []
    for message in dict.fromkeys(warnings):
        (notes if informational(message, card, company) else review).append(message)
    return review, notes
