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
IPC = "https://www.ipc-services.org/sdms/web/rankings/swm"
YEAR = int(os.environ.get("YEAR", datetime.now().year))
ROOT = Path("rankings")
OUT = ROOT / str(YEAR)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_table(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        headers = [clean(x.get_text(" ", strip=True)) for x in table.select("thead th")]
        if not headers:
            first = table.find("tr")
            headers = [clean(x.get_text(" ", strip=True)) for x in first.find_all(["th", "td"])] if first else []
        rows: list[dict[str, str]] = []
        for tr in table.select("tbody tr") or table.find_all("tr")[1:]:
            cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all("td")]
            if len(cells) >= 2:
                rows.append({headers[i] if i < len(headers) else f"column_{i+1}": value for i, value in enumerate(cells)})
        if len(rows) > len(best):
            best = rows
    return best


def set_date(frame, words: list[str], values: list[str]) -> bool:
    for index in range(frame.locator("input").count()):
        element = frame.locator("input").nth(index)
        key = " ".join(filter(None, [element.get_attribute("name"), element.get_attribute("id"), element.get_attribute("placeholder")])).lower()
        if any(word in key for word in words):
            for value in values:
                try:
                    element.fill(value)
                    return True
                except Exception:
                    pass
    return False


def select_option(frame, words: list[str], matcher) -> bool:
    for index in range(frame.locator("select").count()):
        select = frame.locator("select").nth(index)
        key = " ".join(filter(None, [select.get_attribute("name"), select.get_attribute("id")])).lower()
        options = [(o.get_attribute("value") or "", clean(o.inner_text())) for o in select.locator("option").all()]
        if any(word in key for word in words) or any(any(word in text.lower() for word in words) for _, text in options):
            for value, text in options:
                if value and matcher(text.lower()):
                    select.select_option(value)
                    return True
    return False


def submit_form(frame, page) -> None:
    form = frame.locator("form").first
    if not form.count():
        raise RuntimeError("ranking form not found")
    old_html = frame.content()
    try:
        with page.expect_response(
            lambda response: "/sdms/web/rankings/swm" in response.url and response.request.method == "POST",
            timeout=45000,
        ):
            form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
    except Exception:
        form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
    for _ in range(120):
        page.wait_for_timeout(250)
        current = frame.content()
        if current != old_html and (frame.locator("table").count() or "No results" in current):
            break
    page.wait_for_timeout(1000)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    diagnostics: dict[str, object] = {"year": YEAR, "captured_at": datetime.now(timezone.utc).isoformat()}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1200}, locale="en-US")
        page = context.new_page()
        page.goto(PARENT, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)
        iframe = page.locator("iframe[src*='ipc-services.org']").first
        if not iframe.count():
            raise RuntimeError("ranking iframe not found")
        iframe.evaluate("(el,src)=>{el.classList.remove('cookieconsent-optin-statistics');el.src=src}", IPC)
        frame = None
        for _ in range(60):
            page.wait_for_timeout(500)
            frame = next((candidate for candidate in page.frames if "/sdms/web/rankings/swm" in candidate.url), None)
            if frame:
                break
        if not frame:
            raise RuntimeError("ranking frame not loaded")

        diagnostics["controls"] = frame.locator("form input,form select,form button").evaluate_all(
            "els=>els.map(e=>({tag:e.tagName,name:e.name,id:e.id,type:e.type,value:e.value,text:(e.innerText||'').trim()}))"
        )
        diagnostics["form_html"] = frame.locator("form").first.evaluate("el => el.outerHTML")[:50000] if frame.locator("form").count() else ""

        for course, course_label in [("LC", "P50"), ("SC", "P25")]:
            for gender, gender_label in [("M", "Masculino"), ("F", "Femenino")]:
                entry: dict[str, object] = {
                    "year": YEAR, "course": course, "course_label": course_label,
                    "gender": gender, "gender_label": gender_label, "records": 0, "file": ""
                }
                try:
                    frame.goto(IPC, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(700)
                    set_date(frame, ["from", "start", "begin"], [f"{YEAR}-01-01", f"01/01/{YEAR}"])
                    set_date(frame, ["to", "end", "until"], [f"{YEAR}-12-31", f"31/12/{YEAR}"])
                    select_option(frame, ["course", "pool"], lambda text: ("50" in text or "long" in text) if course == "LC" else ("25" in text or "short" in text))
                    select_option(frame, ["gender", "sex"], lambda text: ("male" in text or "men" in text) if gender == "M" else ("female" in text or "women" in text))
                    submit_form(frame, page)
                    rows = parse_table(frame.content())
                    if rows:
                        filename = f"{YEAR}_{course}_{gender}.csv"
                        fields = sorted({key for row in rows for key in row})
                        with (OUT / filename).open("w", newline="", encoding="utf-8") as handle:
                            writer = csv.DictWriter(handle, fieldnames=fields)
                            writer.writeheader()
                            writer.writerows(rows)
                        entry.update({"records": len(rows), "file": f"rankings/{YEAR}/{filename}", "status": "ok"})
                    else:
                        entry["status"] = "sin resultados o filtros no reconocidos"
                except Exception as exc:
                    entry["status"] = str(exc)[:220]
                items.append(entry)
                print(f"{YEAR} {course} {gender}: {entry['records']}")
        browser.close()

    (OUT / "manifest.json").write_text(json.dumps({"year": YEAR, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
