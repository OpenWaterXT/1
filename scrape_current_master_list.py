#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Frame, Page, sync_playwright

PARENT_URL = "https://www.paralympic.org/swimming/classified-athletes"
IPC_URL = "https://www.ipc-services.org/sdms/web/cml/swm"
OUT = Path("data")
SNAPSHOTS = OUT / "snapshots"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def athlete_key(row: dict[str, str]) -> str:
    raw = "|".join(clean(row.get(k, "")).lower() for k in ("name", "npc", "gender"))
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
    for index, cell in enumerate(cells):
        source = clean(headers[index] if index < len(headers) else f"column_{index + 1}").lower()
        field = aliases.get(source, re.sub(r"[^a-z0-9]+", "_", source).strip("_") or f"column_{index + 1}")
        row[field] = clean(cell)
    if not row.get("name"):
        row["name"] = clean(f"{row.get('given_name', '')} {row.get('family_name', '')}")
    row["athlete_id"] = athlete_key(row)
    return row


def extract_table(scope: Page | Frame) -> list[dict[str, str]]:
    best: list[dict[str, str]] = []
    tables = scope.locator("table")
    for table_index in range(tables.count()):
        table = tables.nth(table_index)
        headers = [clean(x) for x in table.locator("thead th").all_inner_texts()]
        if not headers:
            headers = [clean(x) for x in table.locator("tr").first.locator("th,td").all_inner_texts()]
        rows: list[dict[str, str]] = []
        table_rows = table.locator("tbody tr")
        for row_index in range(table_rows.count()):
            cells = [clean(x) for x in table_rows.nth(row_index).locator("td").all_inner_texts()]
            if len(cells) >= 2:
                rows.append(normalise(headers, cells))
        if len(rows) > len(best):
            best = rows
    return best


def accept_cookies(page: Page) -> None:
    for selector in [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Allow all')", "button:has-text('Accept all')",
    ]:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible():
                button.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass


def load_official_frame(page: Page, diagnostics: dict[str, object]) -> Frame | None:
    page.goto(PARENT_URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3500)
    accept_cookies(page)
    iframe = page.locator("iframe[src*='ipc-services.org']").first
    diagnostics["iframe_found"] = bool(iframe.count())
    if not iframe.count():
        return None
    iframe.evaluate("(el, src) => { el.classList.remove('cookieconsent-optin-statistics'); el.src = src; }", IPC_URL)
    for _ in range(30):
        page.wait_for_timeout(1000)
        for frame in page.frames:
            if "ipc-services.org/sdms/web/cml/swm" in frame.url:
                diagnostics["iframe_url"] = frame.url
                diagnostics["iframe_title"] = frame.title()
                return frame
    return None


def options_of(select) -> list[dict[str, str]]:
    return [
        {"value": option.get_attribute("value") or "", "text": clean(option.inner_text())}
        for option in select.locator("option").all()
    ]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    diagnostics: dict[str, object] = {"source": PARENT_URL, "embedded_source": IPC_URL, "captured_at": now.isoformat()}
    rows: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        )
        page = context.new_page()
        responses: list[dict[str, object]] = []
        page.on("response", lambda response: responses.append({
            "url": response.url, "status": response.status,
            "content_type": response.headers.get("content-type", ""),
        }))

        frame = load_official_frame(page, diagnostics)
        if frame is not None:
            page.wait_for_timeout(2500)
            selects = frame.locator("select")
            diagnostics["selects"] = selects.count()
            diagnostics["select_options"] = [options_of(selects.nth(i)) for i in range(selects.count())]

            season_options = options_of(selects.nth(0)) if selects.count() else []
            valid_seasons = [o for o in season_options if o["value"]]
            season = sorted(valid_seasons, key=lambda o: o["text"], reverse=True)[0]["value"] if valid_seasons else "S26"

            # The page has 3 selectors: season, region and NPC. The previous
            # implementation mistakenly iterated the region selector. NPC is #3.
            npc_options = options_of(selects.nth(2)) if selects.count() >= 3 else []
            npcs = [o for o in npc_options if o["value"]]
            diagnostics["season_selected"] = season
            diagnostics["npc_count"] = len(npcs)

            seen: set[str] = set()
            npc_errors: list[dict[str, str]] = []
            for index, npc in enumerate(npcs, 1):
                code = npc["value"].upper()
                try:
                    route = f"{IPC_URL}/html/season/{season.lower()}/npc/{code.lower()}"
                    frame.goto(route, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(250)
                    found = extract_table(frame)
                    for row in found:
                        row["npc"] = row.get("npc") or code
                        signature = json.dumps(row, sort_keys=True, ensure_ascii=False)
                        if signature not in seen:
                            seen.add(signature)
                            rows.append(row)
                    print(f"[{index}/{len(npcs)}] {code}: {len(found)}")
                except Exception as exc:
                    npc_errors.append({"npc": code, "error": str(exc)[:300]})
            diagnostics["npc_errors"] = npc_errors
            diagnostics["frame_tables"] = frame.locator("table").count()
        else:
            diagnostics["frame_error"] = "Official IPC Services iframe did not load"

        diagnostics["rows_extracted"] = len(rows)
        diagnostics["network_candidates"] = [
            item for item in responses
            if any(key in str(item["url"]).lower() for key in ("ipc-services", "cml", "class", "athlete"))
        ][-300:]
        page.screenshot(path=str(OUT / "last_capture.png"), full_page=True)
        browser.close()

    fields = sorted({key for row in rows for key in row}) or [
        "athlete_id", "name", "npc", "gender", "sport_class", "status", "review_date"
    ]
    snapshot = SNAPSHOTS / f"{day}.csv"
    for target in (snapshot, OUT / "latest.csv"):
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    payload = {
        "captured_at": now.isoformat(), "source": PARENT_URL, "embedded_source": IPC_URL,
        "records": len(rows), "fields": fields, "snapshot": str(snapshot), "athletes": rows,
    }
    (OUT / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "capture_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = OUT / "snapshots.json"
    try:
        previous = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        previous = []
    entry = {"date": day, "captured_at": now.isoformat(), "records": len(rows), "file": str(snapshot)}
    previous = [item for item in previous if item.get("date") != day] + [entry]
    previous.sort(key=lambda item: item.get("date", ""), reverse=True)
    index_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")

    print(f"Extracted {len(rows)} classified athletes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
