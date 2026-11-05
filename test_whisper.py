import re, time, requests
from datetime import datetime
from urllib.parse import urlencode
import autoprocesscall as ap

s = requests.Session()
print("login:", ap.login(s), flush=True)

row_re = re.compile(r'<tr\s+class="(?:even|odd)"\s+valign="top">(.*?)</tr>', re.S)
td_re = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
def first_cell(h):
    t = td_re.findall(h)
    return re.sub(r"<[^>]+>|&nbsp;", " ", t[0]).strip() if t else "?"

today = datetime.now().strftime("%Y-%m-%d")
for label, frm, to in [("blank", "", ""), ("today", today, today)]:
    params = {
        "from": frm, "to": to, "seldirection": "", "seldisposition": "answered",
        "reportof": "", "group": "", "txtnum": "", "numbertype": "", "sid": "",
        "pagevalue": "100",
        "fromhr": "00", "frommin": "00", "fromsec": "00",
        "tohr": "23", "tomin": "59", "tosec": "59",
    }
    url = ap.CTI_BASE_URL + "?" + urlencode(params)
    t0 = time.time()
    r = s.get(url, headers=ap.BROWSER_HEADERS, timeout=(10, 120))
    rows = row_re.findall(r.text)
    print(f"{label}: rows={len(rows)} time={time.time()-t0:.1f}s "
          f"first={first_cell(rows[0]) if rows else '-'} "
          f"last={first_cell(rows[-1]) if rows else '-'}", flush=True)
print("DONE", flush=True)