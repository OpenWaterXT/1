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


def parse_ranking_table(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[list[dict[str, str]]] = []
    for table in soup.find_all("table"):
        headers = [clean(x.get_text(" ", strip=True)) for x in table.select("thead th")]
        rows_html = table.select("tbody tr")
        if not headers:
            first = table.find("tr")
            headers = [clean(x.get_text(" ", strip=True)) for x in first.find_all(["th", "td"])] if first else []
            rows_html = table.find_all("tr")[1:]
        rows: list[dict[str, str]] = []
        for tr in rows_html:
            cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all("td")]
            if len(cells) >= 4:
                rows.append({
                    headers[i] if i < len(headers) and headers[i] else f"column_{i+1}": value
                    for i, value in enumerate(cells)
                })
        if rows:
            candidates.append(rows)
    return max(candidates, key=len, default=[])


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


def submit_query(page, frame, year_value: str, course: str, gender: str):
    payload = {
        "year": year_value,
        "course": course,
        "gender": gender,
    }

    with page.expect_response(
        lambda r: "/sdms/web/rankings/swm" in r.url and r.request.method == "POST",
        timeout=120000,
    ) as response_info:
        frame.evaluate(
            """
            ({year, course, gender}) => {
              const form = document.querySelector('form[action*="/rankings/swm"]');
              if (!form) throw new Error('Formulario oficial no encontrado');

              const setSelect = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el) throw new Error(`No existe ${selector}`);
                el.value = value;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              };
              const setRadio = (name, value) => {
                const el = document.querySelector(`input[name="${name}"][value="${value}"]`);
                if (!el) throw new Error(`No existe radio ${name}=${value}`);
                el.checked = true;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              };

              setSelect('#rankings-rankingtypefilter', 'world');
              setSelect('#rankings-rankinglistfilter', year);
              setRadio('Rankings[specificationFilter]', course);
              setRadio('Rankings[genderFilter]', gender);
              setSelect('#rankings-eventtypefilter', '');
              setSelect('#rankings-classfilter', '');

              const button = form.querySelector('button[name="format"][value="html"]');
              if (!button) throw new Error('Botón Show List no encontrado');
              form.requestSubmit(button);
            }
            """,
            payload,
        )

    return response_info.value


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
        options = {
            clean(option.inner_text()): option.get_attribute("value") or ""
            for option in initial_frame.locator("#rankings-rankinglistfilter option").all()
        }
        diagnostics["year_options"] = options
        year_value = options.get(str(YEAR))
        if not year_value:
            raise RuntimeError(f"La temporada {YEAR} no aparece en el formulario oficial")

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
                try:
                    frame = get_form_frame(page)
                    response = submit_query(page, frame, year_value, course, gender_post)
                    html = response.text()
                    rows = parse_ranking_table(html)

                    if not rows:
                        (OUT / f"response_{course}_{gender_file}.html").write_text(html, encoding="utf-8")
                        entry["status"] = f"Sin tabla detectada; HTTP {response.status}"
                    else:
                        filename = f"{YEAR}_{course}_{gender_file}.csv"
                        fields = list(dict.fromkeys(key for row in rows for key in row))
                        with (OUT / filename).open("w", newline="", encoding="utf-8-sig") as handle:
                            writer = csv.DictWriter(handle, fieldnames=fields)
                            writer.writeheader()
                            writer.writerows(rows)
                        entry.update({
                            "records": len(rows),
                            "file": f"rankings/{YEAR}/{filename}",
                            "status": "ok",
                        })
                except Exception as exc:
                    entry["status"] = str(exc)[:500]
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
