#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

PARENT = "https://www.paralympic.org/swimming/rankings"
OUT = Path("rankings/probe")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    diag = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "parent": PARENT,
        "frames": [],
        "requests": [],
        "responses": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        )
        page = context.new_page()

        page.on("request", lambda r: diag["requests"].append({
            "method": r.method,
            "url": r.url,
            "post_data": (r.post_data or "")[:10000],
        }) if "ipc-services.org" in r.url else None)
        page.on("response", lambda r: diag["responses"].append({
            "status": r.status,
            "url": r.url,
            "content_type": r.headers.get("content-type", ""),
        }) if "ipc-services.org" in r.url else None)

        page.goto(PARENT, wait_until="networkidle", timeout=120000)
        page.wait_for_timeout(5000)

        for selector in [
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button:has-text('Allow all')",
            "button:has-text('Accept all')",
        ]:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click()
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        iframe_count = page.locator("iframe").count()
        diag["iframe_count"] = iframe_count
        diag["iframe_attributes"] = [
            {
                "src": page.locator("iframe").nth(i).get_attribute("src"),
                "class": page.locator("iframe").nth(i).get_attribute("class"),
                "title": page.locator("iframe").nth(i).get_attribute("title"),
            }
            for i in range(iframe_count)
        ]

        page.wait_for_timeout(5000)
        diag["frames"] = [{"url": f.url, "name": f.name} for f in page.frames]

        for idx, frame in enumerate(page.frames):
            try:
                html = frame.content()
                (OUT / f"frame_{idx}.html").write_text(html, encoding="utf-8")
                controls = frame.locator("input,select,button,form").evaluate_all(
                    "els => els.map(e => ({tag:e.tagName,name:e.name,id:e.id,type:e.type,value:e.value,text:(e.innerText||'').trim(),action:e.action||''}))"
                )
                diag.setdefault("frame_controls", []).append({"url": frame.url, "controls": controls})
            except Exception as exc:
                diag.setdefault("frame_errors", []).append({"url": frame.url, "error": str(exc)})

        (OUT / "parent.html").write_text(page.content(), encoding="utf-8")
        browser.close()

    (OUT / "diagnostics.json").write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "iframe_count": diag.get("iframe_count"),
        "frames": diag.get("frames"),
        "ipc_requests": len(diag.get("requests", [])),
        "ipc_responses": len(diag.get("responses", [])),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
