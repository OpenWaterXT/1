#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PARENT='https://www.paralympic.org/swimming/classified-athletes'
IPC='https://www.ipc-services.org/sdms/web/cml/swm'
OUT=Path('data'); SEASONS=OUT/'seasons'
REGIONS={'AFR','AMR','ASR','OCR'}

def clean(s): return re.sub(r'\s+',' ',s or '').strip()
def key(r):
    raw='|'.join(clean(r.get(k,'')).lower() for k in ('season','sdms_id','name','npc','gender'))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
def parse(html):
    soup=BeautifulSoup(html,'html.parser'); best=[]
    for table in soup.find_all('table'):
        hs=table.select('thead th')
        if not hs:
            tr=table.find('tr'); hs=tr.find_all(['th','td']) if tr else []
        headers=[clean(x.get_text(' ',strip=True)).lower() for x in hs]
        aliases={'sdms id':'sdms_id','family name':'family_name','given name':'given_name','gender':'gender','s':'s','sb':'sb','sm':'sm','status':'status','exceptions':'exceptions','npc':'npc'}
        rows=[]
        trs=table.select('tbody tr') or table.find_all('tr')[1:]
        for tr in trs:
            cells=[clean(x.get_text(' ',strip=True)) for x in tr.find_all('td')]
            if len(cells)<2: continue
            r={}
            for i,v in enumerate(cells):
                h=headers[i] if i<len(headers) else f'column_{i+1}'
                r[aliases.get(h,re.sub(r'[^a-z0-9]+','_',h).strip('_'))]=v
            r['name']=clean(f"{r.get('given_name','')} {r.get('family_name','')}")
            rows.append(r)
        if len(rows)>len(best): best=rows
    return best

def write_csv(path, rows):
    fields=sorted({k for r in rows for k in r})
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

OUT.mkdir(exist_ok=True); SEASONS.mkdir(parents=True,exist_ok=True)
now=datetime.now(timezone.utc).isoformat()
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    c=b.new_context(locale='en-US',user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36')
    page=c.new_page(); page.goto(PARENT,wait_until='domcontentloaded',timeout=120000); page.wait_for_timeout(3000)
    for s in ['#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',"button:has-text('Allow all')","button:has-text('Accept all')"]:
        try:
            x=page.locator(s).first
            if x.count() and x.is_visible(): x.click(); page.wait_for_timeout(1000); break
        except: pass
    iframe=page.locator("iframe[src*='ipc-services.org']").first
    iframe.evaluate("(el,src)=>{el.classList.remove('cookieconsent-optin-statistics');el.src=src}",IPC)
    frame=None
    for _ in range(50):
        page.wait_for_timeout(400)
        frame=next((f for f in page.frames if 'ipc-services.org/sdms/web/cml/swm' in f.url),None)
        if frame: break
    if not frame: raise RuntimeError('IPC frame not loaded')
    sels=frame.locator('select')
    npc_opts=[{'value':o.get_attribute('value') or '','text':clean(o.inner_text())} for o in sels.nth(2).locator('option').all()]
    npcs=[o for o in npc_opts if re.fullmatch(r'[A-Za-z]{3}',o['value']) and o['value'].upper() not in REGIONS]
    rows=[]; seen=set(); counts={}
    for i,npc in enumerate(npcs,1):
        code=npc['value'].upper()
        if frame.url.rstrip('/')!=IPC.rstrip('/'):
            frame.goto(IPC,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(250)
        ss=frame.locator('select'); ss.nth(0).select_option('S25'); ss.nth(1).select_option(''); ss.nth(2).select_option(npc['value'])
        html=frame.evaluate("""async()=>{const f=document.querySelector('form');const fd=new FormData(f);fd.set('ClassificationMasterList[seasonFilter]','S25');fd.set('ClassificationMasterList[regionFilter]','');fd.set('ClassificationMasterList[organisationFilter]',document.querySelector('#classificationmasterlist-organisationfilter').value);fd.set('format','html');const r=await fetch(f.action,{method:'POST',body:fd,credentials:'include'});return await r.text()}""")
        found=parse(html); counts[code]=len(found)
        for r in found:
            r['npc']=r.get('npc') or code; r['season']='S25'; r['season_label']='Summer Season 2025'; r['athlete_id']=key(r)
            sig=r.get('sdms_id') or json.dumps(r,sort_keys=True,ensure_ascii=False)
            if sig not in seen: seen.add(sig); rows.append(r)
        print(f'[{i}/{len(npcs)}] {code}: {len(found)}')
    b.close()
if len(rows)<1000: raise RuntimeError(f'S25 incomplete: only {len(rows)} records')
write_csv(SEASONS/'S25.csv',rows)
(SEASONS/'S25.json').write_text(json.dumps({'season':'S25','label':'Summer Season 2025','captured_at':now,'records':len(rows),'athletes':rows},ensure_ascii=False,indent=2),encoding='utf-8')
# Merge with existing S26
all_rows=list(rows); index=[{'season':'S25','label':'Summer Season 2025','records':len(rows),'json':'data/seasons/S25.json','csv':'data/seasons/S25.csv'}]
s26=SEASONS/'S26.json'
if s26.exists():
    p=json.loads(s26.read_text(encoding='utf-8')); a=p.get('athletes',[]); all_rows=a+all_rows
    index.insert(0,{'season':'S26','label':p.get('label','Summer Season 2026'),'records':len(a),'json':'data/seasons/S26.json','csv':'data/seasons/S26.csv'})
(OUT/'seasons.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf-8')
write_csv(OUT/'latest.csv',all_rows)
(OUT/'latest.json').write_text(json.dumps({'captured_at':now,'source':PARENT,'records':len(all_rows),'seasons':index,'athletes':all_rows},ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'s25_diagnostics.json').write_text(json.dumps({'captured_at':now,'records':len(rows),'npc_counts':counts},ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Complete S25: {len(rows)} records')
