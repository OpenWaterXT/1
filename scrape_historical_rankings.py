#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PARENT = "https://www.paralympic.org/swimming/rankings"
YEAR = int(os.environ["YEAR"])
OUT = Path("rankings") / str(YEAR)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def option_map(frame, selector: str) -> dict[str, str]:
    return {
        clean(option.inner_text()): option.get_attribute("value") or ""
        for option in frame.locator(f"{selector} option").all()
    }


def find_all_option(options: dict[str, str]) -> tuple[str, str] | None:
    for label, value in options.items():
        normalized = clean(label).lower()
        if normalized in {"all", "all events", "all event", "all classes", "all class", "todos", "todas"}:
            return value, label
        if normalized.startswith("all ") or normalized.startswith("todas ") or normalized.startswith("todos "):
            return value, label
    return None


def nearby_heading(table) -> str:
    caption = table.find("caption")
    if caption:
        text = clean(caption.get_text(" ", strip=True))
        if text:
            return text
    heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong"])
    return clean(heading.get_text(" ", strip=True)) if heading else ""


def parse_ranking_tables(
    html: str,
    event_label: str = "",
    class_label: str = "",
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    all_rows: list[dict[str, str]] = []

    for table_index, table in enumerate(soup.find_all("table"), start=1):
        headers = [clean(x.get_text(" ", strip=True)) for x in table.select("thead th")]
        rows_html = table.select("tbody tr")
        if not headers:
            first = table.find("tr")
            headers = [clean(x.get_text(" ", strip=True)) for x in first.find_all(["th", "td"])] if first else []
            rows_html = table.find_all("tr")[1:]

        section = nearby_heading(table)
        for tr in rows_html:
            cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all("td")]
            if len(cells) < 4:
                continue
            row = {
                headers[i] if i < len(headers) and headers[i] else f"column_{i+1}": value
                for i, value in enumerate(cells)
            }
            row["Event"] = event_label or section
            row["Class"] = class_label
            row["Section"] = section
            row["Table"] = str(table_index)
            all_rows.append(row)

    return all_rows


def get_form_frame(page):
    page.goto(PARENT, wait_until="domcontentloaded", timeout=120000)
    for selector in [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Accept all')",
        "button:has-text('Allow all')",
    ]:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible():
                button.click()
                break
        except Exception:
            pass

    iframe = page.locator("iframe[src*='ipc-services.org'][src*='rankings/swm']").first
    iframe.wait_for(state="attached", timeout=60000)
    iframe.evaluate("el => el.classList.remove('cookieconsent-optin-statistics')")
    frame = iframe.content_frame
    if frame is None:
        raise RuntimeError("No se pudo abrir el iframe oficial")
    frame.locator("#rankings-rankinglistfilter").wait_for(state="attached", timeout=120000)
    return frame


def submit_query(
    page,
    frame,
    year_value: str,
    course: str,
    gender: str,
    event_value: str,
    class_value: str,
) -> tuple[str, int]:
    payload = {
        "year": year_value,
        "course": course,
        "gender": gender,
        "event": event_value,
        "sportClass": class_value,
    }
    form = frame.locator("form[action*='/rankings/swm']").first
    form.wait_for(state="attached", timeout=60000)

    with page.expect_response(
        lambda r: "/sdms/web/rankings/swm" in r.url and r.request.method == "POST",
        timeout=120000,
    ) as response_info:
        form.evaluate(
            """
            (form, {year, course, gender, event, sportClass}) => {
              const setSelect = (selector, value) => {
                const el = form.querySelector(selector);
                if (!el) throw new Error(`No existe ${selector}`);
                el.value = value;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              };
              const setRadio = (name, value) => {
                const el = form.querySelector(`input[name="${name}"][value="${value}"]`);
                if (!el) throw new Error(`No existe radio ${name}=${value}`);
                el.checked = true;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              };

              setSelect('#rankings-rankingtypefilter', 'world');
              setSelect('#rankings-rankinglistfilter', year);
              setRadio('Rankings[specificationFilter]', course);
              setRadio('Rankings[genderFilter]', gender);
              setSelect('#rankings-eventtypefilter', event);
              setSelect('#rankings-classfilter', sportClass);

              const button = form.querySelector('button[name="format"][value="html"]');
              if (!button) throw new Error('Botón Show List no encontrado');
              form.requestSubmit(button);
            }
            """,
            payload,
        )

    post_response = response_info.value
    html = ""
    for _ in range(240):
        page.wait_for_timeout(500)
        try:
            html = frame.locator("html").evaluate("el => el.outerHTML")
            lower = html.lower()
            if "<table" in lower or "no result" in lower or "no ranking" in lower:
                break
        except Exception:
            continue

    if not html:
        raise RuntimeError("La redirección terminó sin HTML accesible en el iframe")
    return html, post_response.status


def unique_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, str]] = []
    for row in rows:
        key = tuple(sorted((k, v) for k, v in row.items() if k not in {"Table", "Section"}))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    diagnostics: dict[str, object] = {
        "year": YEAR,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": PARENT,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        )
        page = context.new_page()

        initial_frame = get_form_frame(page)
        year_options = option_map(initial_frame, "#rankings-rankinglistfilter")
        event_options = option_map(initial_frame, "#rankings-eventtypefilter")
        class_options = option_map(initial_frame, "#rankings-classfilter")
        diagnostics["year_options"] = year_options
        diagnostics["event_options"] = event_options
        diagnostics["class_options"] = class_options

        year_value = year_options.get(str(YEAR))
        if not year_value:
            raise RuntimeError(f"La temporada {YEAR} no aparece en el formulario oficial")

        all_event = find_all_option(event_options)
        all_class = find_all_option(class_options)
        event_queries = [all_event] if all_event else [(value, label) for label, value in event_options.items() if value]
        class_queries = [all_class] if all_class else [(value, label) for label, value in class_options.items() if value]

        if not event_queries:
            raise RuntimeError("El formulario no contiene pruebas seleccionables")
        if not class_queries:
            raise RuntimeError("El formulario no contiene clases seleccionables")

        for course, course_label in [("LC", "P50"), ("SC", "P25")]:
            for gender_post, gender_file, gender_label in [
                ("M", "M", "Masculino"),
                ("W", "F", "Femenino"),
            ]:
                entry: dict[str, object] = {
                    "year": YEAR,
                    "course": course,
                    "course_label": course_label,
                    "gender": gender_file,
                    "gender_label": gender_label,
                    "records": 0,
                    "file": "",
                }
                combined_rows: list[dict[str, str]] = []
                errors: list[str] = []

                for event_value, event_label in event_queries:
                    for class_value, class_label in class_queries:
                        try:
                            frame = get_form_frame(page)
                            html, _ = submit_query(
                                page,
                                frame,
                                year_value,
                                course,
                                gender_post,
                                event_value,
                                class_value,
                            )
                            combined_rows.extend(
                                parse_ranking_tables(html, event_label=event_label, class_label=class_label)
                            )
                        except Exception as exc:
                            errors.append(f"{event_label} / {class_label}: {str(exc)[:180]}")

                rows = unique_rows(combined_rows)
                if rows:
                    filename = f"{YEAR}_{course}_{gender_file}.csv"
                    preferred = ["Event", "Class", "Rank", "Name", "NPC", "Birth", "Time", "Date", "City", "Country", "Section", "Table"]
                    all_fields = list(dict.fromkeys(key for row in rows for key in row))
                    fields = [field for field in preferred if field in all_fields] + [field for field in all_fields if field not in preferred]
                    with (OUT / filename).open("w", newline="", encoding="utf-8-sig") as handle:
                        writer = csv.DictWriter(handle, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(rows)
                    entry.update({
                        "records": len(rows),
                        "file": f"rankings/{YEAR}/{filename}",
                        "status": "ok",
                    })
                else:
                    entry["status"] = "; ".join(errors[:4]) or "Sin tablas de ranking detectadas"

                items.append(entry)
                print(entry)

        browser.close()

    (OUT / "manifest.json").write_text(
        json.dumps({"year": YEAR, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not any(int(item.get("records", 0)) > 0 for item in items):
        raise RuntimeError(f"La temporada {YEAR} terminó sin ningún ranking descargado")


if __name__ == "__main__":
    main()
