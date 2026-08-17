import urllib.request
import json

def get(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json'
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode('utf-8')
        return json.loads(text)

params = [
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&size=80',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&limit=80',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&pageSize=80',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&page_size=80',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&items=80',
    'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1&perPage=80',
]

for url in params:
    print('URL:', url)
    try:
        data = get(url)
        pag = data['data']['pagination']
        print('total', pag.get('total'), 'pages', pag.get('totalPages'))
        print('private', len(data['data'].get('private', [])), 'agency', len(data['data'].get('agency', [])), 'yad1', len(data['data'].get('yad1', [])))
    except Exception as e:
        print('ERROR', e)
    print()
