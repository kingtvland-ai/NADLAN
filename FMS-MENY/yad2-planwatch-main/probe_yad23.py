import requests

resp = requests.get("http://127.0.0.1:8080/api/yad23", params={}, timeout=60)
print(resp.status_code)
print(resp.text[:5000])
