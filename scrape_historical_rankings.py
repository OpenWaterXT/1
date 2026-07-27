#!/usr/bin/env python3
from __future__ import annotations

import csv, json, re
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PARENT='https://www.paralympic.org/swimming/rankings'
IPC='https://www.ipc-services.org/sdms/web/ranking/sw/'
OUT=Path('rankings')
START_YEAR=2009
END_YEAR=datetime.now().year


def clean(s): return re.sub(r'\s+',' ',s or '').strip()

def parse_table(html):
    soup=BeautifulSoup(html,'html.parser'); best=[]
    for table in soup.find_all('table'):
        hs=[clean(x.get_text(' ',strip=True)) for x in table.select('thead th')]
        if not hs:
            first=table.find('tr'); hs=[clean(x.get_text(' ',strip=True)) for x in first.find_all(['th','td'])] if first else []
        rows=[]
        for tr in (table.select('tbody tr') or table.find_all('tr')[1:]):
            cs=[clean(x.get_text(' ',strip=True)) for x in tr.find_all('td')]
            if len(cs)>=2: rows.append({(hs[i] if i<len(hs) else f'column_{i+1}'):v for i,v in enumerate(cs)})
        if len(rows)>len(best): best=rows
    return best

def set_date(frame, words, value):
    for i in range(frame.locator('input').count()):
        el=frame.locator('input').nth(i)
        key=' '.join(filter(None,[el.get_attribute('name'),el.get_attribute('id'),el.get_attribute('placeholder')])).lower()
        if any(w in key for w in words):
            try: el.fill(value); return True
            except: pass
    return False

def select_by_text(frame, words, desired):
    for i in range(frame.locator('select').count()):
        sel=frame.locator('select').nth(i)
        key=' '.join(filter(None,[sel.get_attribute('name'),sel.get_attribute('id')])).lower()
        opts=[(o.get_attribute('value') or '',clean(o.inner_text())) for o in sel.locator('option').all()]
        if any(w in key for w in words) or any(any(w in t.lower() for w in words) for _,t in opts):
            for v,t in opts:
                if desired(t.lower()):
                    sel.select_option(v); return True
    return False

def main():
    OUT.mkdir(exist_ok=True)
    manifest=[]; diagnostics=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(accept_downloads=True,viewport={'width':1600,'height':1200},locale='en-US')
        page=context.new_page(); page.goto(PARENT,wait_until='domcontentloaded',timeout=120000); page.wait_for_timeout(3000)
        iframe=page.locator("iframe[src*='ipc-services.org']").first
        if not iframe.count(): raise RuntimeError('ranking iframe not found')
        iframe.evaluate('(el,src)=>{el.src=src}',IPC)
        frame=None
        for _ in range(60):
            page.wait_for_timeout(500)
            frame=next((f for f in page.frames if '/sdms/web/ranking/sw' in f.url),None)
            if frame: break
        if not frame: raise RuntimeError('ranking frame not loaded')
        diagnostics.append({'controls':frame.locator('form input,form select,form button').evaluate_all("els=>els.map(e=>({tag:e.tagName,name:e.name,id:e.id,type:e.type,value:e.value,text:(e.innerText||'').trim()}))")})
        for year in range(START_YEAR,END_YEAR+1):
            for course,label_course in [('LC','P50'),('SC','P25')]:
                for gender,label_gender in [('M','Masculino'),('F','Femenino')]:
                    try:
                        frame.goto(IPC,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(500)
                        set_date(frame,['from','start','begin'],f'{year}-01-01') or set_date(frame,['from','start','begin'],f'01/01/{year}')
                        set_date(frame,['to','end','until'],f'{year}-12-31') or set_date(frame,['to','end','until'],f'31/12/{year}')
                        select_by_text(frame,['course','pool'],lambda t: ('50' in t or 'long' in t) if course=='LC' else ('25' in t or 'short' in t))
                        select_by_text(frame,['gender','sex'],lambda t: ('male' in t or 'men' in t) if gender=='M' else ('female' in t or 'women' in t))
                        stem=f'{year}_{course}_{gender}'; target=OUT/f'{stem}.csv'
                        button=frame.locator("button[name='format'][value='html'],button:has-text('Show'),button:has-text('Search')").first
                        if not button.count(): button=frame.locator("button[type='submit']").first
                        button.click(); page.wait_for_timeout(1800)
                        rows=parse_table(frame.content())
                        if rows:
                            fields=sorted({k for r in rows for k in r})
                            with target.open('w',newline='',encoding='utf-8') as fh:
                                w=csv.DictWriter(fh,fieldnames=fields); w.writeheader(); w.writerows(rows)
                            manifest.append({'year':year,'course':course,'course_label':label_course,'gender':gender,'gender_label':label_gender,'records':len(rows),'file':str(target)})
                        else:
                            manifest.append({'year':year,'course':course,'course_label':label_course,'gender':gender,'gender_label':label_gender,'records':0,'file':'','status':'sin resultados o filtro no reconocido'})
                    except Exception as exc:
                        manifest.append({'year':year,'course':course,'course_label':label_course,'gender':gender,'gender_label':label_gender,'records':0,'file':'','status':str(exc)[:180]})
        browser.close()
    payload={'generated_at':datetime.now(timezone.utc).isoformat(),'source':PARENT,'years':[START_YEAR,END_YEAR],'items':manifest}
    (OUT/'manifest.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'diagnostics.json').write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding='utf-8')
    print('generated',len(manifest),'ranking blocks')

if __name__=='__main__': main()
