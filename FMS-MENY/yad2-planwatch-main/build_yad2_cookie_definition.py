import json
import re
from pathlib import Path

cookie_file = Path(r'C:\Users\menym\.codex\attachments\0ad8ace6-9f9e-4bc7-9aea-b57dd95d13d1\pasted-text.txt')
out = Path('projetcsaleyad2.json')
cookies = {}
for line in cookie_file.read_text(encoding='utf-8', errors='ignore').splitlines():
    if not line or line.startswith('#'):
        continue
    parts = line.split('\t')
    if len(parts) >= 7:
        if re.fullmatch(r'[A-Za-z0-9_\-]+', parts[5]) and parts[6].strip():
            cookies[parts[5]] = parts[6].strip()
definition = {
    'name': 'projetcsaleyad2', 'method': 'GET',
    'url': 'https://www.yad2.co.il/realestate/forsale',
    'response_type': 'rendered_html', 'headers': {'user-agent': 'Mozilla/5.0'},
    'cookies': cookies,
    'pagination': {'enabled': True, 'param': 'page', 'start': 1, 'max_pages': 50, 'stop_when_empty': True},
    'render': {'wait_until': 'domcontentloaded', 'timeout_ms': 30000, 'block_assets': True, 'bypass_anti_bot': True, 'stealth': True},
}
out.write_text(json.dumps(definition, ensure_ascii=False), encoding='utf-8')
print('cookies_loaded', len(cookies), 'definition', out)
