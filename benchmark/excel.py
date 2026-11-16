"""Read input only: ASTRA columns, aliases, hyperlinks, stable row identities."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openpyxl import load_workbook

ALIASES = {
    "name": ["название компании", "наименование компании", "наименование", "company", "company name"],
    "legal_name": ["полное название компании", "полное наименование"],
    "inn": ["инн", "inn"],
    "ogrn": ["огрн", "огрнип"],
    "kpp": ["кпп"],
    "address": ["адрес", "юридический адрес"],
    "region": ["название региона", "регион"],
    "phone": ["телефон"],
    "email": ["электронный адрес", "email", "e-mail"],
    "website": ["электронная страница", "сайт", "website", "url", "веб-сайт"],
    "refined_website": ["уточненный адрес электронной страницы", "уточнённый адрес электронной страницы"],
    "okved": ["код оквэд кдес ред. 2", "оквэд", "код оквэд"],
    "activity": ["описание оквэд кдес ред. 2 на русском языке", "вид деятельности"],
}


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def header(value) -> str:
    return re.sub(r"\s+", " ", text(value).lower().rstrip(":"))


def normalize_url(value: str) -> str | None:
    value = value.strip().rstrip(".,;")
    if not value or "@" in value:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or "." not in p.hostname:
            return None
        if p.username or p.password or re.search(r"\s", value):
            return None
        p.port  # Reject malformed ports.
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except ValueError:
        return None


def urls_from_cell(cell) -> list[str]:
    candidates = []
    if cell.hyperlink and cell.hyperlink.target:
        candidates.append(cell.hyperlink.target)
    value = text(cell.value)
    formula = re.match(r'^=HYPERLINK\(\s*"([^"]+)"', value, re.I)
    if formula:
        candidates.append(formula.group(1))
    elif not value.startswith("="):
        candidates.extend(re.split(r"[;\s\n]+", value))
    return list(dict.fromkeys(u for v in candidates if (u := normalize_url(v))))


def column_map(row) -> dict[str, int]:
    values = [header(c.value) for c in row]
    mapping = {}
    for key, aliases in ALIASES.items():
        for alias in aliases:
            if alias in values:
                mapping[key] = values.index(alias)
                break
    return mapping


def inspect_workbook(path: Path, sheet: str | None = None, header_row: int | None = None):
    """Caller owns returned workbook. Fail on ambiguous sheets instead of guessing."""
    wb = load_workbook(path, data_only=False)
    try:
        if sheet and sheet not in wb.sheetnames:
            raise ValueError(f"Лист {sheet!r} не найден. Есть: {', '.join(wb.sheetnames)}")
        candidates = []
        for ws in ([wb[sheet]] if sheet else wb.worksheets):
            rows = [header_row] if header_row else range(1, min(ws.max_row, 30) + 1)
            for rn in rows:
                mapping = column_map(ws[rn])
                if "name" in mapping and ("inn" in mapping or "website" in mapping):
                    candidates.append((ws, rn, mapping))
                    break
        if not candidates:
            raise ValueError("Не найдены заголовки компаний. Укажите --sheet и --header-row.")
        if len(candidates) > 1:
            raise ValueError("Несколько подходящих листов: " + ", ".join(c[0].title for c in candidates) + ". Укажите --sheet.")
        return wb, candidates[0]
    except BaseException:
        wb.close()
        raise


def read_companies(path: Path, sheet: str | None = None, header_row: int | None = None):
    wb, (ws, rn, mapping) = inspect_workbook(path, sheet, header_row)
    records = []
    try:
        for row in ws.iter_rows(min_row=rn + 1):
            fields = {key: text(row[col].value) for key, col in mapping.items()
                      if key not in {"website", "refined_website"}}
            if not fields.get("name"):
                continue
            if any(v.startswith("=") for v in fields.values()):
                raise ValueError(f"Формула в идентификационных данных: {ws.title}!{row[0].row}. Сохраните значения.")
            urls = {}
            for key in ["refined_website", "website"]:
                urls[key] = urls_from_cell(row[mapping[key]]) if key in mapping else []
            payload = {"sheet": ws.title, "row": row[0].row, **fields, **urls}
            payload["company_id"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()[:20]
            records.append(payload)
    finally:
        wb.close()
    if not records:
        raise ValueError("На выбранном листе нет компаний.")
    return records
