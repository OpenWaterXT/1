#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import Frame, Page, sync_playwright

PARENT_URL = "https://www.paralympic.org/swimming/classified-athletes"
IPC_URL = "https://www.ipc-services.org/sdms/web/cml/swm"
OUT = Path("data")
SEASONS_DIR = OUT / "seasons"
SNAPSHOTS = OUT / "snapshots"
REGIONS = {"AFR", "AMR", "ASR", "OCR"}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def athlete_key(row: dict[str, str]) -> str:
    raw = "|".join(clean(row.get(k, "")).lower() for k in ("season", "sdms_id", "name", "npc", "gender"))
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


def accept_cookies(page: Page) -> None:
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


def wait_for_frame(page: Page) -> Frame | None:
    for _ in range(50):
        page.wait_for_timeout(400)
        frame = next((f for f in page.frames if "ipc-services.org/sdms/web/cml/swm" in f.url), None)
        if frame:
            return frame
    return None


def options(select) -> list[dict[str, str]]:
    return [
        {"value": option.get_attribute("value") or "", "text": clean(option.inner_text())}
        for option in select.locator("option").all()
    ]


def click_show_list(frame: Frame, page: Page) -> None:
    button = frame.locator("button[name='format'][value='html']").first
    if not button.count():
        button = frame.locator("form button[type='submit']").first
    if not button.count():
        raise RuntimeError("Show List button not found")
    old_url = frame.url
    button.click()
    for _ in range(120):
        page.wait_for_timeout(250)
        if frame.url != old_url or frame.locator("table tbody tr").count() > 0:
            break
    page.wait_for_timeout(700)


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    SEASONS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    diagnostics: dict[str, object] = {
        "source": PARENT_URL,
        "embedded_source": IPC_URL,
        "captured_at": now.isoformat(),
    }
    combined: list[dict[str, str]] = []
    season_index: list[dict[str, object]] = []

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
        available = [o for o in options(selects.nth(0)) if o["value"]]
        diagnostics["available_seasons"] = available
        if not available:
            raise RuntimeError("No seasons available")

        for season_pos, season_info in enumerate(available, 1):
            code = season_info["value"].upper()
            label = season_info["text"] or code
            try:
                if frame.url.rstrip("/") != IPC_URL.rstrip("/"):
                    frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(500)
                current = frame.locator("select")
                current.nth(0).select_option(season_info["value"])
                current.nth(1).select_option("")
                current.nth(2).select_option("")
                click_show_list(frame, page)
                rows = parse_table(frame.content())

                # Some deployments require one NPC at a time. Fall back only when
                # the all-members request returns no rows.
                if not rows:
                    frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(400)
                    npc_options = [
                        o for o in options(frame.locator("select").nth(2))
                        if re.fullmatch(r"[A-Za-z]{3}", o["value"] or "") and o["value"].upper() not in REGIONS
                    ]
                    seen_ids: set[str] = set()
                    fallback_rows: list[dict[str, str]] = []
                    for npc in npc_options:
                        if frame.url.rstrip("/") != IPC_URL.rstrip("/"):
                            frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000)
                            page.wait_for_timeout(250)
                        current = frame.locator("select")
                        current.nth(0).select_option(season_info["value"])
                        current.nth(2).select_option(npc["value"])
                        click_show_list(frame, page)
                        for row in parse_table(frame.content()):
                            row["npc"] = row.get("npc") or npc["value"].upper()
                            signature = row.get("sdms_id") or json.dumps(row, sort_keys=True, ensure_ascii=False)
                            if signature not in seen_ids:
                                seen_ids.add(signature)
                                fallback_rows.append(row)
                    rows = fallback_rows

                for row in rows:
                    row["season"] = code
                    row["season_label"] = label
                    row["athlete_id"] = athlete_key(row)

                fields = sorted({key for row in rows for key in row})
                write_csv(SEASONS_DIR / f"{code}.csv", rows, fields)
                (SEASONS_DIR / f"{code}.json").write_text(json.dumps({
                    "season": code,
                    "label": label,
                    "captured_at": now.isoformat(),
                    "records": len(rows),
                    "fields": fields,
                    "athletes": rows,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                combined.extend(rows)
                season_index.append({
                    "season": code,
                    "label": label,
                    "records": len(rows),
                    "json": f"data/seasons/{code}.json",
                    "csv": f"data/seasons/{code}.csv",
                })
                print(f"[{season_pos}/{len(available)}] {code}: {len(rows)}")
            except Exception as exc:
                diagnostics.setdefault("season_errors", []).append({"season": code, "error": str(exc)[:500]})

        browser.close()

    diagnostics["seasons_downloaded"] = season_index
    diagnostics["rows_extracted"] = len(combined)
    (OUT / "capture_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    if not combined:
        raise RuntimeError("No season returned athletes; previous dataset preserved")

    season_index.sort(key=lambda item: str(item["season"]), reverse=True)
    (OUT / "seasons.json").write_text(json.dumps(season_index, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = sorted({key for row in combined for key in row})
    write_csv(OUT / "latest.csv", combined, fields)
    write_csv(SNAPSHOTS / f"{day}.csv", combined, fields)
    (OUT / "latest.json").write_text(json.dumps({
        "captured_at": now.isoformat(),
        "source": PARENT_URL,
        "embedded_source": IPC_URL,
        "records": len(combined),
        "seasons": season_index,
        "fields": fields,
        "snapshot": f"data/snapshots/{day}.csv",
        "athletes": combined,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = OUT / "snapshots.json"
    try:
        previous = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        previous = []
    entry = {"date": day, "captured_at": now.isoformat(), "records": len(combined), "file": f"data/snapshots/{day}.csv"}
    previous = [item for item in previous if item.get("date") != day] + [entry]
    previous.sort(key=lambda item: item.get("date", ""), reverse=True)
    index_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")
    print(f"Extracted {len(combined)} records across {len(season_index)} seasons")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
