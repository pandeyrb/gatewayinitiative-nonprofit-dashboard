import requests
from unified_crawler_starter import HEADERS, REQUEST_TIMEOUT

r = requests.get("https://brhousing.org", headers=HEADERS, timeout=REQUEST_TIMEOUT)
print(r.status_code)
print(r.url)
print(len(r.text))