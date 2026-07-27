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
            if frame and frame.locator("#rankings-rankinglistfilter").count():
                break
        if not frame or not frame.locator("#rankings-rankinglistfilter").count():
            raise RuntimeError("ranking form not loaded")

        year_select = frame.locator("#rankings-rankinglistfilter")
        year_options = {
            clean(option.inner_text()): option.get_attribute("value") or ""
            for option in year_select.locator("option").all()
        }
        diagnostics["year_options"] = year_options
        year_value = year_options.get(str(YEAR))
        if not year_value:
            raise RuntimeError(f"ranking year {YEAR} is not available")

        for course, course_label in [("LC", "P50"), ("SC", "P25")]:
            for gender_submit, gender_label, gender_file in [("M", "Masculino", "M"), ("W", "Femenino", "F")]:
                entry: dict[str, object] = {
                    "year": YEAR,
                    "course": course,
                    "course_label": course_label,
                    "gender": gender_file,
                    "gender_label": gender_label,
                    "records": 0,
                    "file": "",
                }
                try:
                    frame.goto(IPC, wait_until="domcontentloaded", timeout=60000)
                    frame.locator("#rankings-rankingtypefilter").select_option("world")
                    frame.locator("#rankings-rankinglistfilter").select_option(year_value)
                    frame.locator(f"input[name='Rankings[specificationFilter]'][value='{course}']").check()
                    frame.locator(f"input[name='Rankings[genderFilter]'][value='{gender_submit}']").check()
                    frame.locator("#rankings-eventtypefilter").select_option("")
                    frame.locator("#rankings-classfilter").select_option("")

                    old_html = frame.content()
                    with page.expect_response(
                        lambda response: "/sdms/web/rankings/swm" in response.url and response.request.method == "POST",
                        timeout=60000,
                    ):
                        frame.locator("button[name='format'][value='html']").click()

                    for _ in range(120):
                        page.wait_for_timeout(250)
                        if frame.content() != old_html and frame.locator("table").count():
                            break
                    page.wait_for_timeout(1000)

                    html = frame.content()
                    rows = parse_table(html)
                    if rows:
                        filename = f"{YEAR}_{course}_{gender_file}.csv"
                        fields = sorted({key for row in rows for key in row})
                        with (OUT / filename).open("w", newline="", encoding="utf-8") as handle:
                            writer = csv.DictWriter(handle, fieldnames=fields)
                            writer.writeheader()
                            writer.writerows(rows)
                        entry.update({"records": len(rows), "file": f"rankings/{YEAR}/{filename}", "status": "ok"})
                    else:
                        (OUT / f"response_{course}_{gender_file}.html").write_text(html, encoding="utf-8")
                        entry["status"] = "respuesta recibida, pero no se detectó la tabla"
                except Exception as exc:
                    entry["status"] = str(exc)[:220]
                items.append(entry)
                print(f"{YEAR} {course} {gender_file}: {entry['records']}")

        browser.close()

    (OUT / "manifest.json").write_text(json.dumps({"year": YEAR, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
