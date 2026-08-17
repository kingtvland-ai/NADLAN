import requests
base='http://127.0.0.1:8080/api/yad23'
for params in ({'url':'https://www.yad2.co.il/sitemaps-v2/yad2/realestate/forsale/new-region_1.xml'},{'target':'https://www.yad2.co.il/sitemaps-v2/yad2/realestate/forsale/new-region_1.xml'},{'type':'forsale'},{'category':'forsale'}):
    try:
        r=requests.get(base,params=params,timeout=60)
        print(params,r.status_code,r.text[:300])
    except Exception as e: print(params,e)
