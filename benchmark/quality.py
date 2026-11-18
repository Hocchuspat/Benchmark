"""Report readiness depends on company facts, independently of site ownership."""
from __future__ import annotations

import re

REPORT_FIELDS = {"product_service_type": "Продукция / услуги", "primary_activity": "Вид деятельности",
                 "functions_summary": "Функции компании"}


def unresolved_scope_issue(message):
    """Keep explicit admissions of mixed-company facts, including legacy cards."""
    text = " ".join(message.casefold().replace("ё", "е").split())
    if any(word in text for word in ["не перенес", "не использован", "исключены сведения", "конфликт не установлен"]):
        return False
    business_facts = bool(re.search(r"деятельност|функци|профил|продукц|производств|проектир|услуг|работ", text))
    return bool(
        ("групп" in text and (business_facts or "сведения относятся к группе" in text)
         and any(term in text for term in ["принадлежность", "относятся", "не подтверждены отдельно"]))
        or business_facts and "исключительная принадлежность" in text
        or re.search(r"(деятельност|функци|продукци|услуг).{0,100}(друг[а-я]+ юридическ|чуж[а-я]+ юридическ|нельзя отнести)", text)
        or re.search(r"(противореч|конфликт).{0,80}(деятельност|функци|идентификац|целев.{0,12}инн)", text)
    )


def classify_warnings(warnings, card, company):
    # New cards have explicit report issues. Legacy warnings remain available,
    # but technical observations and arbitrary prose no longer block a report.
    review = list(dict.fromkeys(s.strip() for s in card.get("report_issues", []) if s.strip()))
    notes = []
    for message in dict.fromkeys(warnings):
        if unresolved_scope_issue(message):
            if message not in review:
                review.append(message)
        elif message not in review:
            notes.append(message)
    return review, notes
