#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.paralympic.org/swimming/classified-athletes"
OUT = Path("data")
SNAPSHOTS = OUT / "snapshots"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def key_for(row: dict[str, str]) -> str:
    preferred = [row.get("name", ""), row.get("npc", ""), row.get("gender", "")]
    raw = "|".join(clean(x).lower() for x in preferred)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalise(headers: list[str], cells: list[str]) -> dict[str, str]:
    aliases = {
        "athlete": "name", "athlete name": "name", "name": "name",
        "family name": "family_name", "given name": "given_name",
        "npc": "npc", "country": "npc", "nation": "npc",
        "gender": "gender", "sex": "gender",
        "sport class": "sport_class", "class": "sport_class",
        "status": "status", "sport class status": "status",
        "review date": "review_date", "fixed review date": "review_date", "frd": "review_date",
    }
    row: dict[str, str] = {}
    for i, cell in enumerate(cells):
        source = clean(headers[i] if i < len(headers) else f"column_{i+1}").lower()
        field = aliases.get(source, re.sub(r"[^a-z0-9]+", "_", source).strip("_") or f"column_{i+1}")
        row[field] = clean(cell)
    if not row.get("name"):
        row["name"] = clean(" ".join([row.get("given_name", ""), row.get("family_name", "")]))
    row["athlete_id"] = key_for(row)
    return row


def extract_table(page) -> list[dict[str, str]]:
    page.wait_for_timeout(5000)
    tables = page.locator("table")
    best: list[dict[str, str]] = []
    for t in range(tables.count()):
        table = tables.nth(t)
        headers = [clean(x) for x in table.locator("thead th").all_inner_texts()]
        if not headers:
            first = table.locator("tr").first
            headers = [clean(x) for x in first.locator("th,td").all_inner_texts()]
        rows: list[dict[str, str]] = []
        body_rows = table.locator("tbody tr")
        if body_rows.count() == 0:
            body_rows = table.locator("tr").nth(1)
        for i in range(body_rows.count()):
            cells = [clean(x) for x in body_rows.nth(i).locator("td").all_inner_texts()]
            if len(cells) >= 2:
                rows.append(normalise(headers, cells))
        if len(rows) > len(best):
            best = rows
    return best


def click_all_or_expand(page) -> None:
    for selector in [
        "text=Show all", "text=View all", "button:has-text('All')",
        "select[name*='length']", "select[aria-label*='rows']"
    ]:
        try:
            loc = page.locator(selector).first
            if loc.count():
                if loc.evaluate("e => e.tagName === 'SELECT'"):
                    options = loc.locator("option").all()
                    if options:
                        loc.select_option(options[-1].get_attribute("value"))
                else:
                    loc.click()
                page.wait_for_timeout(2500)
        except Exception:
            pass


def main() -> int:
    OUT.mkdir(exist_ok=True)
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    date_id = stamp.strftime("%Y-%m-%d")
    diagnostics: dict[str, object] = {"source": URL, "captured_at": stamp.isoformat()}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        responses: list[dict[str, object]] = []
        page.on("response", lambda r: responses.append({"url": r.url, "status": r.status, "content_type": r.headers.get("content-type", "")}))
        page.goto(URL, wait_until="networkidle", timeout=120000)
        click_all_or_expand(page)
        rows = extract_table(page)
        diagnostics["title"] = page.title()
        diagnostics["tables"] = page.locator("table").count()
        diagnostics["rows_extracted"] = len(rows)
        diagnostics["network_candidates"] = [r for r in responses if any(k in str(r["url"]).lower() for k in ("class", "athlete", "swim", "api", "json"))][-100:]
        page.screenshot(path=str(OUT / "last_capture.png"), full_page=True)
        (OUT / "last_page.html").write_text(page.content(), encoding="utf-8")
        browser.close()

    fields = sorted({k for row in rows for k in row.keys()}) or ["athlete_id", "name", "npc", "gender", "sport_class", "status", "review_date"]
    snapshot_csv = SNAPSHOTS / f"{date_id}.csv"
    for target in [snapshot_csv, OUT / "latest.csv"]:
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    payload = {
        "captured_at": stamp.isoformat(),
        "source": URL,
        "records": len(rows),
        "fields": fields,
        "snapshot": str(snapshot_csv),
        "athletes": rows,
    }
    (OUT / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "capture_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    index_file = OUT / "snapshots.json"
    previous = []
    if index_file.exists():
        try:
            previous = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception:
            previous = []
    entry = {"date": date_id, "captured_at": stamp.isoformat(), "records": len(rows), "file": str(snapshot_csv)}
    previous = [x for x in previous if x.get("date") != date_id] + [entry]
    previous.sort(key=lambda x: x.get("date", ""), reverse=True)
    index_file.write_text(json.dumps(previous, indent=2), encoding="utf-8")

    print(f"Extracted {len(rows)} classified athletes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
