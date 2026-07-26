#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

URL="https://www.ipc-services.org/sdms/web/cml/swm"
OUT=Path("data"); SNAPSHOTS=OUT/"snapshots"
def clean(v): return re.sub(r"\s+"," ",(v or "")).strip()
def key_for(r): return hashlib.sha1("|".join(clean(r.get(k,"" )).lower() for k in ("name","npc","gender")).encode()).hexdigest()[:16]
def normalise(headers,cells):
 aliases={"athlete":"name","athlete name":"name","name":"name","family name":"family_name","given name":"given_name","npc":"npc","country":"npc","nation":"npc","gender":"gender","sex":"gender","sport class":"sport_class","class":"sport_class","status":"status","sport class status":"status","review date":"review_date","fixed review date":"review_date","frd":"review_date"}
 r={}
 for i,c in enumerate(cells):
  h=clean(headers[i] if i<len(headers) else f"column_{i+1}").lower(); k=aliases.get(h,re.sub(r"[^a-z0-9]+","_",h).strip("_") or f"column_{i+1}"); r[k]=clean(c)
 if not r.get("name"): r["name"]=clean(" ".join((r.get("given_name",""),r.get("family_name",""))))
 r["athlete_id"]=key_for(r); return r
def extract(page):
 best=[]
 for t in range(page.locator("table").count()):
  table=page.locator("table").nth(t); headers=[clean(x) for x in table.locator("thead th").all_inner_texts()]
  if not headers: headers=[clean(x) for x in table.locator("tr").first.locator("th,td").all_inner_texts()]
  rows=[]; trs=table.locator("tbody tr")
  for i in range(trs.count()):
   cells=[clean(x) for x in trs.nth(i).locator("td").all_inner_texts()]
   if len(cells)>=2: rows.append(normalise(headers,cells))
  if len(rows)>len(best): best=rows
 return best
def submit(page):
 for sel in ["button[type=submit]","input[type=submit]","button:has-text('Search')","button:has-text('View')"]:
  try:
   x=page.locator(sel).first
   if x.count(): x.click(); page.wait_for_load_state("networkidle",timeout=60000); page.wait_for_timeout(1200); return
  except Exception: pass

def main():
 OUT.mkdir(exist_ok=True); SNAPSHOTS.mkdir(parents=True,exist_ok=True); now=datetime.now(timezone.utc); day=now.strftime("%Y-%m-%d")
 diag={"source":URL,"captured_at":now.isoformat()}; rows=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True); page=browser.new_page(viewport={"width":1600,"height":1200}); responses=[]
  page.on("response",lambda r: responses.append({"url":r.url,"status":r.status,"content_type":r.headers.get("content-type","")}))
  page.goto(URL,wait_until="networkidle",timeout=120000); page.wait_for_timeout(2500)
  selects=page.locator("select"); diag["selects"]=selects.count(); diag["select_options"]=[]
  for i in range(selects.count()):
   s=selects.nth(i); opts=[{"value":o.get_attribute("value") or "","text":clean(o.inner_text())} for o in s.locator("option").all()]; diag["select_options"].append(opts)
  # Choose the latest/non-placeholder season in the first select.
  if selects.count():
   opts=diag["select_options"][0]; valid=[o for o in opts if o["value"] and "select" not in o["text"].lower()]
   if valid: selects.nth(0).select_option(valid[-1]["value"])
  # Iterate every NPC when a second select exists; otherwise submit once.
  npc_values=[""]
  if selects.count()>1:
   opts=diag["select_options"][1]; npc_values=[o["value"] for o in opts if o["value"] and "select" not in o["text"].lower()] or [""]
  seen=set()
  for npc in npc_values:
   try:
    if selects.count()>1 and npc: selects.nth(1).select_option(npc)
    submit(page); found=extract(page)
    for r in found:
     if not r.get("npc") and npc: r["npc"]=npc
     signature=json.dumps(r,sort_keys=True)
     if signature not in seen: seen.add(signature); rows.append(r)
   except Exception: continue
  diag.update({"title":page.title(),"tables":page.locator("table").count(),"rows_extracted":len(rows),"network_candidates":[r for r in responses if any(k in r["url"].lower() for k in ("cml","class","athlete","api","json"))][-200:]})
  page.screenshot(path=str(OUT/"last_capture.png"),full_page=True); (OUT/"last_page.html").write_text(page.content(),encoding="utf-8"); browser.close()
 fields=sorted({k for r in rows for k in r}) or ["athlete_id","name","npc","gender","sport_class","status","review_date"]
 snap=SNAPSHOTS/f"{day}.csv"
 for target in (snap,OUT/"latest.csv"):
  with target.open("w",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
 payload={"captured_at":now.isoformat(),"source":URL,"records":len(rows),"fields":fields,"snapshot":str(snap),"athletes":rows}; (OUT/"latest.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); (OUT/"capture_diagnostics.json").write_text(json.dumps(diag,ensure_ascii=False,indent=2),encoding="utf-8")
 idx=OUT/"snapshots.json"; previous=[]
 try: previous=json.loads(idx.read_text()) if idx.exists() else []
 except Exception: pass
 entry={"date":day,"captured_at":now.isoformat(),"records":len(rows),"file":str(snap)}; previous=[x for x in previous if x.get("date")!=day]+[entry]; previous.sort(key=lambda x:x.get("date",""),reverse=True); idx.write_text(json.dumps(previous,indent=2),encoding="utf-8")
 print(f"Extracted {len(rows)} classified athletes")
if __name__=="__main__": main()
