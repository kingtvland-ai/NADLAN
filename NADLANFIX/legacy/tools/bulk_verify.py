import json,glob,os,sys
c=glob.glob(r'F:\AI-STUDIO-BUILDER-APP\PROJECT-CITY\*-API')[0]
c2a=os.path.join(c,'curl2api');sys.path.insert(0,c2a);os.chdir(c2a)
import core.engine as engine
from core import storage
res=[]
seen=set()
for s in storage.list_scrapers():
 try:
  name=s if isinstance(s,str) else s.get('name')
  if not name or name in seen: continue
  seen.add(name)
  d=storage.load_scraper(name)
  r=engine.run_scraper(d)
  data=r.get('data');dl=data if isinstance(data,list) else ([data] if data else [])
  img=None
  if dl:
   f=dl[0]
   for k in ('image','coverImage','img','images','thumbnail'):
    if isinstance(f,dict) and f.get(k):img=k;break
  res.append({'name':name,'status':r.get('status_code'),'records':len(dl),'img':img})
  print(f"[OK] {name}: {r.get('status_code')} records={len(dl)} img={img}")
 except Exception as e:
  nm=name if 'name' in locals() and name else (s if isinstance(s,str) else (s.get('name') if isinstance(s,dict) else str(s)))
  res.append({'name':nm,'error':str(e)[:120]})
  print(f"[FAIL] {nm}: {e}")
print("=== SUMMARY ===")
for x in res:
 if 'error' in x:print("FAIL",x['name'],x['error'])
 elif x['records']>0:print("OK",x['name'],x['records'],"records","img:",x['img'])
 else:print("EMPTY",x['name'])
json.dump(res,open('bulk_result.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
print("Saved bulk_result.json")
