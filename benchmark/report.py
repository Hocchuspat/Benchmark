"""Rebuild reports from committed state. Input workbook is never overwritten."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from .storage import now

FACT_LABELS = {
    "legal_name": "Юридическое название (найдено)", "inn": "ИНН (найдено)",
    "ogrn": "ОГРН/ОГРНИП (найдено)", "kpp": "КПП (найдено)",
    "legal_address": "Юридический адрес (найдено)", "actual_address": "Фактический адрес (найдено)",
    "phone": "Телефон (найдено)", "email": "Email (найдено)", "okved": "ОКВЭД (найдено)",
    "primary_activity": "Вид деятельности", "additional_activities": "Дополнительная деятельность",
    "products_services": "Продукты и услуги", "product_service_type": "продукция/услуга",
    "functional_profile": "Функциональный профиль", "functions_summary": "Функции",
}
STATES = {"pending": "В очереди", "running": "Обрабатывается", "completed": "Обработано",
          "needs_review": "Требует проверки", "failed": "Ошибка обработки"}
RESEARCH = {"completed": "Исследование завершено", "needs_review": "Нужна проверка",
            "site_unavailable": "Нет доступа к сайту", "not_found": "Сайт не найден", "ambiguous": "Неоднозначность"}


def safe(value):
    """Prevent input/website content becoming spreadsheet formulas in XLSX and CSV."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def atomic_path(path, writer):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".export-", suffix=path.suffix)
    os.close(fd)
    try:
        writer(Path(tmp))
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def write_csv(path, headers, rows):
    def writer(tmp):
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            csv_writer = csv.writer(handle, delimiter=";")
            csv_writer.writerow(headers)
            csv_writer.writerows([[safe(v) for v in row] for row in rows])
    atomic_path(path, writer)


def report_tables(store):
    headers = ["Название компании", "ИНН", "Электронная страница", "Оквэд", "продукция/услуга", "Вид деятельности", "Функции",
               "ID компании", "Лист источника", "Строка источника", "Статус очереди", "Статус исследования", "Принадлежность сайта",
               "Основание принадлежности", "Исходные сайты", "Уточнённые сайты", "Входной адрес", "Входной ОГРН", "ОКВЭД (входной)"]
    remaining = [k for k in FACT_LABELS if k not in {"okved", "product_service_type", "primary_activity", "functions_summary"}]
    headers += [FACT_LABELS[k] for k in remaining]
    headers += ["Недостающие сведения", "Замечания", "Ошибка обработки", "Попытки", "Обновлено UTC"]
    source_headers = ["ID компании", "Название компании", "ID источника", "URL", "Название страницы", "Дата доступа", "Тип", "Подтверждающий фрагмент", "Подтверждает поля", "Объяснения фактов"]
    rows, source_rows = [], []
    for item in store.rows():
        company = json.loads(item["input_json"])
        card = json.loads(item["card_json"]) if item["card_json"] else {}
        quality = json.loads(item["quality_json"]) if item["quality_json"] else {}
        facts = card.get("facts", {})
        def fact(key):
            return facts.get(key, {}).get("value") or ""
        rows.append([company["name"], company.get("inn", ""), card.get("official_site") or "", fact("okved"),
                     fact("product_service_type"), fact("primary_activity"), fact("functions_summary"),
                     company["company_id"], company["sheet"], company["row"], STATES[item["state"]], RESEARCH.get(card.get("research_status"), ""),
                     card.get("identity", {}).get("status", ""), card.get("identity", {}).get("reason", ""),
                     "\n".join(company.get("website", [])), "\n".join(company.get("refined_website", [])),
                     company.get("address", ""), company.get("ogrn", ""), company.get("okved", ""), *[fact(k) for k in remaining],
                     ", ".join(FACT_LABELS.get(k, k) for k in card.get("missing_information", [])),
                     "\n".join(quality.get("warnings", [])), item["error"] or "", item["attempts"], item["updated_at"]])
        for source in card.get("sources", []):
            linked = [k for k, v in facts.items() if source["source_id"] in v["source_ids"]]
            if source["source_id"] in card["identity"]["source_ids"]:
                linked.append("identity")
            notes = [f"{FACT_LABELS[k]}: {facts[k]['note']}" for k in linked if k in facts and facts[k]["note"]]
            source_rows.append([company["company_id"], company["name"], source["source_id"], source["url"], source["title"],
                                source["accessed_at"], source["kind"], source["excerpt"], ", ".join(linked), "\n".join(notes)])
    return headers, rows, source_headers, source_rows


def style_sheet(ws, widths):
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    line = Side(style="thin", color="D9D9D9")
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 44
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Calibri", size=11)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=line)
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F2F6FA")
        # Variable height: long research text must not be silently clipped.
        max_lines = max((sum(max(1, (len(part) + widths.get(c.column, 28) - 1) // widths.get(c.column, 28))
                             for part in str(c.value or "").split("\n")) for c in row), default=1)
        ws.row_dimensions[row[0].row].height = min(409, max(32, 16 * (max_lines + 1)))
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = widths.get(col, 28)


def export_reports(store):
    headers, rows, source_headers, source_rows = report_tables(store)
    folder = store.run_dir / "reports"
    write_csv(folder / "report.csv", headers, rows)
    write_csv(folder / "sources.csv", source_headers, source_rows)
    def write_workbook(path):
        wb = Workbook()
        ws = wb.active
        ws.title = "Досье компаний"
        main_indices = list(range(7)) + [headers.index(label) for label in ["Статус исследования", "Принадлежность сайта", "Статус очереди", "ID компании"]]
        ws.append([headers[i] for i in main_indices])
        for row in rows:
            ws.append([safe(row[i]) for i in main_indices])
        style_sheet(ws, {1: 32, 2: 16, 3: 36, 4: 36, 5: 20, 6: 65, 7: 75, 8: 28, 9: 28, 10: 24, 11: 24})
        details = wb.create_sheet("Реквизиты и контроль")
        details.append(headers)
        for row in rows:
            details.append([safe(v) for v in row])
        widths = {i: 42 for i in range(1, len(headers) + 1)}
        widths.update({1: 32, 2: 16, 3: 36, 4: 36, 5: 20, 6: 65, 7: 75, 8: 24, 9: 24, 10: 12, 11: 24, 12: 28, 13: 28})
        for label in ["Основание принадлежности", "Входной адрес", "Юридический адрес (найдено)",
                      "Фактический адрес (найдено)", "Продукты и услуги", "Дополнительная деятельность",
                      "Недостающие сведения", "Замечания", "Ошибка обработки"]:
            widths[headers.index(label) + 1] = 70
        widths[headers.index("Попытки") + 1] = 12
        widths[headers.index("Обновлено UTC") + 1] = 25
        style_sheet(details, widths)
        sources = wb.create_sheet("Источники")
        sources.append(source_headers)
        for row in source_rows:
            sources.append([safe(v) for v in row])
        style_sheet(sources, {1: 24, 2: 30, 3: 14, 4: 65, 5: 35, 6: 14, 7: 14, 8: 65, 9: 40, 10: 65})
        wb.save(path)
        wb.close()
    atomic_path(folder / "report.xlsx", write_workbook)
    metrics = pilot_metrics(store)
    atomic_path(folder / "pilot.json", lambda p: p.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"))
    return folder


def pilot_metrics(store):
    metrics = {"generated_at": now(), **store.summary(), "cards": 0, "confirmed_sites": 0,
               "research_statuses": {}, "source_count": 0, "populated_facts": 0,
               "accuracy_measured": False,
               "subscription_remaining": None,
               "note": "Токены — диагностический расход CLI, не процент лимита подписки. Правильность фактов требует независимой проверки."}
    metrics["web_calls"] = 0
    metrics["excluded_sources"] = 0
    metrics["excluded_facts"] = 0
    durations = []
    for attempt in store.db.execute("SELECT started_at,finished_at FROM attempts WHERE finished_at IS NOT NULL"):
        durations.append((datetime.fromisoformat(attempt[1]) - datetime.fromisoformat(attempt[0])).total_seconds())
    metrics["wall_seconds_attempts"] = sum(durations)
    metrics["uncached_input_tokens"] = max(0, metrics["usage"]["input_tokens"] - metrics["usage"]["cached_input_tokens"])
    for row in store.rows():
        if not row["card_json"]:
            continue
        card = json.loads(row["card_json"])
        quality = json.loads(row["quality_json"])
        metrics["cards"] += 1
        metrics["confirmed_sites"] += card["identity"]["status"].startswith("confirmed")
        status = card["research_status"]
        metrics["research_statuses"][status] = metrics["research_statuses"].get(status, 0) + 1
        metrics["source_count"] += quality["source_count"]
        metrics["populated_facts"] += quality["populated_facts"]
        metrics["web_calls"] += quality.get("web_calls", 0)
        metrics["excluded_sources"] += len(quality.get("provenance", {}).get("excluded_source_ids", []))
        metrics["excluded_facts"] += len(quality.get("provenance", {}).get("excluded_fact_fields", []))
        metrics["excluded_facts"] += len(quality.get("entity_scope", {}).get("excluded_fact_fields", []))
    return metrics
