#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PARENT_URL = "https://www.paralympic.org/swimming/classified-athletes"
IPC_URL = "https://www.ipc-services.org/sdms/web/cml/swm"
OUT = Path("data")
SNAPSHOTS = OUT / "snapshots"
REGIONS = {"AFR", "AMR", "ASR", "EUR", "OCR"}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def athlete_key(row: dict[str, str]) -> str:
    raw = "|".join(clean(row.get(k, "")).lower() for k in ("sdms_id", "name", "npc", "gender"))
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
        "sdms id": "sdms_id", "id": "sdms_id",
        "s": "s", "sb": "sb", "sm": "sm", "exceptions": "exceptions",
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


def parse_table(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        header_nodes = table.select("thead th")
        if not header_nodes:
            first = table.find("tr")
            header_nodes = first.find_all(["th", "td"]) if first else []
        headers = [clean(node.get_text(" ", strip=True)) for node in header_nodes]
        rows: list[dict[str, str]] = []
        body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
        for tr in body_rows:
            cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) >= 2:
                rows.append(normalise(headers, cells))
        if len(rows) > len(best):
            best = rows
    return best


def accept_cookies(page) -> None:
    for selector in [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Allow all')", "button:has-text('Accept all')",
    ]:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible():
                button.click()
                page.wait_for_timeout(1200)
                return
        except Exception:
            pass


def wait_for_frame(page):
    for _ in range(40):
        page.wait_for_timeout(500)
        frame = next((f for f in page.frames if "ipc-services.org/sdms/web/cml/swm" in f.url), None)
        if frame:
            return frame
    return None


def submit_current_form(frame, page) -> None:
    form = frame.locator("form").first
    if not form.count():
        raise RuntimeError("IPC form not found")
    old_url = frame.url
    form.evaluate("form => form.submit()")
    for _ in range(40):
        page.wait_for_timeout(250)
        if frame.url != old_url or frame.locator("table").count():
            break
    page.wait_for_timeout(300)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    diagnostics: dict[str, object] = {
        "source": PARENT_URL,
        "embedded_source": IPC_URL,
        "captured_at": now.isoformat(),
    }
    rows: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        )
        page = context.new_page()
        page.goto(PARENT_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)
        accept_cookies(page)

        iframe = page.locator("iframe[src*='ipc-services.org']").first
        if not iframe.count():
            raise RuntimeError("Official IPC Services iframe not found")
        iframe.evaluate("(el, src) => { el.classList.remove('cookieconsent-optin-statistics'); el.src = src; }", IPC_URL)

        frame = wait_for_frame(page)
        if frame is None:
            raise RuntimeError("Official IPC Services iframe did not load")

        selects = frame.locator("select")
        if selects.count() < 3:
            raise RuntimeError(f"Expected 3 selectors, found {selects.count()}")

        season_options = [
            {"value": o.get_attribute("value") or "", "text": clean(o.inner_text())}
            for o in selects.nth(0).locator("option").all()
        ]
        valid_seasons = [o for o in season_options if o["value"]]
        season = sorted(valid_seasons, key=lambda o: o["text"], reverse=True)[0]["value"] if valid_seasons else "S26"

        npc_options = [
            {"value": o.get_attribute("value") or "", "text": clean(o.inner_text())}
            for o in selects.nth(2).locator("option").all()
        ]
        npcs = [
            o for o in npc_options
            if o["value"].upper() not in REGIONS and re.fullmatch(r"[A-Za-z]{3}", o["value"] or "")
        ]
        diagnostics.update({"season_selected": season, "npc_count": len(npcs), "npcs": npcs})

        seen: set[str] = set()
        errors: list[dict[str, str]] = []
        counts: dict[str, int] = {}

        for index, npc in enumerate(npcs, 1):
            code = npc["value"].upper()
            try:
                # Return to the official form before each request so the selectors
                # and CSRF/session state are always fresh.
                if frame.url.rstrip("/") != IPC_URL.rstrip("/"):
                    frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(350)

                current_selects = frame.locator("select")
                if current_selects.count() < 3:
                    raise RuntimeError(f"Expected 3 selectors, found {current_selects.count()}")
                current_selects.nth(0).select_option(season)
                current_selects.nth(2).select_option(npc["value"])
                submit_current_form(frame, page)

                found = parse_table(frame.content())
                counts[code] = len(found)
                for row in found:
                    row["npc"] = code
                    row["athlete_id"] = athlete_key(row)
                    signature = row.get("sdms_id") or json.dumps(row, sort_keys=True, ensure_ascii=False)
                    if signature not in seen:
                        seen.add(signature)
                        rows.append(row)
                print(f"[{index}/{len(npcs)}] {code}: {len(found)}")
            except Exception as exc:
                errors.append({"npc": code, "error": str(exc)[:300]})

        diagnostics["npc_counts"] = counts
        diagnostics["npc_errors"] = errors
        diagnostics["rows_extracted"] = len(rows)
        diagnostics["final_frame_url"] = frame.url
        browser.close()

    # Never overwrite a valid dataset with an empty capture.
    if not rows:
        diagnostics["preserved_previous_dataset"] = True
        (OUT / "capture_diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise RuntimeError("Capture returned zero athletes; previous dataset preserved")

    fields = sorted({key for row in rows for key in row})
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
