"""
autoprocesscall.py

Polls the Enjay Synapse CTI call-reports page for newly ANSWERED calls that
have a recording, and submits each one (one at a time, in order) to the
local Call Summary web app's /summarize endpoint using the recording's
direct audio URL - exactly like pasting the URL into the "Paste audio file
URL" box on the web page, but automatically.

This can be used in TWO ways:

1. STANDALONE (unchanged behaviour):
       python autoprocesscall.py

2. EMBEDDED inside web_app.py, running as a background thread in the same
   process / same Windows service ("Call Summery Tool"). web_app.py does:

       import autoprocesscall
       threading.Thread(target=autoprocesscall.run_forever, daemon=True).start()

It keeps track of which recordings it has already processed in
processed_calls.json (next to this script), so restarting the script/service
will not re-process the same calls.
"""

import json
import re
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests

# ---------------------------------------------------------------------------
# CONFIGURATION - edit these to match your setup
# ---------------------------------------------------------------------------

CTI_LOGIN_URL = "http://192.168.200.188:6751/newcti/login.php"
CTI_INDEX_URL = "http://192.168.200.188:6751/newcti/index.php"
CTI_BASE_URL = "http://192.168.200.188:6751/newcti/reports/callreports.php"
WEB_APP_SUMMARIZE_URL = "http://127.0.0.1:8080/summarize" 

CTI_USERNAME = "admin"
CTI_PASSWORD = "Sandip!@#987654"
# Exact form field names the login form posts (confirm via DevTools ->
# Network -> login.php -> Payload/Form Data if login fails).
CTI_USERNAME_FIELD = "username"
CTI_PASSWORD_FIELD = "pass"

POLL_INTERVAL_SECONDS = 10 * 60     
PAGE_SIZE = 50                      
SHOW_TRANSCRIPT = True              
DEBUG_LOGIN = True                  

# Only process calls whose executive extension/reference matches one of
# these. Any call from an extension NOT in this list is omitted entirely.
ALLOWED_REFERENCES = [
    "45",   # Kishor Shinde
    "52",   # Amol Jadhav
    "13",   # Suresh Kolekar
    "12",   # Sandeep Patil
    "17",   # Deepak Jalinder
    "10",   # Sandeep Agale
    "17",   # Deepak Labade
    "15",   # Rahul Budhwant
    "43",   # Vaibhav Rajguru
    "39",   # Satish Kharade
    "58",   # Sagar Deshmukh
    "55",   # Tejesh Yadav
    "63",   # Akshay Deshmukh
    "70",   # Gautam Shinde
    "71",   # Vijay Chaudhari
    "75",   # Avinash Kore
    "81",   # Dilip Aarey
    "104",  # Vidya Nandurdikar
    "95",   # Mahendra Walunj
    "109",  # Pratik Sathe
    "100",  # Kishor Avhad
    "101",  # Vidya Korade
    "103",  # Akash Badadhe
    "112",  # Prathamesh Gaikwad
    "123",  # Nivrutti Chaudhari
    "127",  # Ganesh Pise
]  
LOOKBACK_MINUTES = 10              

STATE_FILE = Path(__file__).resolve().parent / "processed_calls.json"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def parse_call_datetime(call_date_str):
    """Parse a call_date string like '2026-07-28 09:44:20' into a
    datetime object. Returns None if it can't be parsed."""
    try:
        return datetime.strptime(call_date_str.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None

def get_extension(text):
    """Extract the leading numeric extension/reference from a source or
    destination field like '101 (Vidya Korade)' -> '101', or
    '+919844441786 (AGARWAL...)' -> '919844441786'."""
    if not text:
        return None
    m = re.match(r'^\+?(\d+)', text.strip())
    return m.group(1) if m else None



def load_processed():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_processed(processed):
    STATE_FILE.write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def login(session):
    print(f"[AUTO_POLLER][DEBUG] Sending username={CTI_USERNAME!r} (len={len(CTI_USERNAME)}), "
          f"password length={len(CTI_PASSWORD)}")

    session.get(CTI_LOGIN_URL, headers=BROWSER_HEADERS, timeout=30)
    time.sleep(3)

    resp = session.post(
        CTI_LOGIN_URL,
        data={
            CTI_USERNAME_FIELD: CTI_USERNAME,
            CTI_PASSWORD_FIELD: CTI_PASSWORD,
            "login": "Login",
        },
        headers={**BROWSER_HEADERS, "Referer": CTI_LOGIN_URL},
        timeout=30,
    )
    resp.raise_for_status()

    if DEBUG_LOGIN:
        print("---- LOGIN DEBUG ----")
        print("Status code:", resp.status_code)
        print("Cookies after login:", session.cookies.get_dict())
        print("Response length:", len(resp.text))
        print("'location.replace(\\'index.php\\')' in text? ->",
              "location.replace('index.php')" in resp.text)
        print("'name=\"pass\"' in text? ->", 'name="pass"' in resp.text.lower())
        print("---- END LOGIN DEBUG ----")

    # Check success marker FIRST (see explanation above).
    if "location.replace('index.php')" in resp.text or 'location.replace("index.php")' in resp.text:
        pass  # treat as success, fall through to verification below
    elif 'name="pass"' in resp.text.lower():
        return False

    # Positively confirm via index.php's dashboard frame, same as before.
    verify_resp = session.get(CTI_INDEX_URL, headers=BROWSER_HEADERS, timeout=30)
    verify_resp.raise_for_status()

    if DEBUG_LOGIN:
        print("---- VERIFY (index.php) DEBUG ----")
        print("'dashboard.php' in text? ->", "dashboard.php" in verify_resp.text)
        print("---- END VERIFY DEBUG ----")

    if "dashboard.php" in verify_resp.text:
        return True

    return False


def is_logged_out(html):
    """Heuristic: the CTI login page re-appears if the session expired."""
    return 'name="pass"' in html.lower()


def fetch_call_rows(session):
    """Fetch the CTI call-reports page (Answered calls, today) and return a
    list of dicts, oldest first, each with the recording URL if present."""
    params = {
        "from": "",
        "to": "",
        "seldirection": "",
        "seldisposition": "answered",   
        "reportof": "",                 
        "group": "",
        "txtnum": "",
        "numbertype": "",
        "sid": "",
        "pagevalue": str(PAGE_SIZE),
        "fromhr": "00",
        "frommin": "00",
        "fromsec": "00",
        "tohr": "23",
        "tomin": "59",
        "tosec": "59",
    }
    url = CTI_BASE_URL + "?" + urlencode(params)
    resp = session.get(url, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    debug_path = Path(__file__).resolve().parent / "debug_callreports.html"
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[DEBUG] Saved raw HTML to debug_callreports.html (length={len(html)}, 'ANSWERED' in it? {'ANSWERED' in html})")

    if is_logged_out(html):
        print("[AUTH] Session expired / not logged in - logging in again...")
        if not login(session):
            raise RuntimeError("CTI login failed - check CTI_USERNAME / "
                                "CTI_PASSWORD / field names.")
        resp = session.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
        if is_logged_out(html):
            raise RuntimeError("Still on login page after logging in - "
                                "check CTI_USERNAME_FIELD / CTI_PASSWORD_FIELD.")

    rows = []
    row_re = re.compile(r'<tr\s+class="(?:even|odd)"\s+valign="top">(.*?)</tr>', re.S)
    td_re = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
    href_re = re.compile(r"href='([^']+\.wav)'")

    for row_html in row_re.findall(html):
        tds = td_re.findall(row_html)
        if len(tds) < 7:
            continue

        def clean(t):
            t = re.sub(r"<[^>]+>", " ", t)          # strip tags
            t = t.replace("&nbsp;", " ")
            return re.sub(r"\s+", " ", t).strip()

        call_date = clean(tds[0])
        source = clean(tds[1])
        destination = clean(tds[2])
        disposition = clean(tds[3])
        call_type = clean(tds[4])
        duration = clean(tds[5])

        if "ANSWERED" not in disposition.upper():
            continue

        m = href_re.search(tds[6])
        if not m:
            continue  # answered but no recording available - skip

        # Determine the executive's extension: it's in the SOURCE field for
        # outgoing calls, and in the DESTINATION field for incoming calls.
        if call_type.strip().lower() == "incoming":
            extension = get_extension(destination)
        else:
            extension = get_extension(source)

        if ALLOWED_REFERENCES and extension not in ALLOWED_REFERENCES:
            continue  # not an allowed reference -> omit this call entirely

        recording_url = urljoin(url, m.group(1))

        rows.append({
            "call_date": call_date,
            "source": source,
            "destination": destination,
            "disposition": disposition,
            "type": call_type,
            "duration": duration,
            "recording_url": recording_url,
        })

    rows.reverse()  # page lists newest first; we process oldest -> newest
    return rows


def submit_to_web_app(call):
    print(f"[PROCESS] {call['call_date']} | {call['source']} -> "
          f"{call['destination']} | {call['duration']} | "
          f"{call['recording_url']}")
    data = {
        "audio_url": call["recording_url"],
        "show_transcript": "1" if SHOW_TRANSCRIPT else "",
    }
    resp = requests.post(WEB_APP_SUMMARIZE_URL, data=data, timeout=600)
    if resp.status_code != 200:
        print(f"[ERROR] web_app returned status {resp.status_code} "
              f"for {call['recording_url']}")
        return False
    print(f"[DONE] {call['recording_url']}")
    return True


def run_forever():
   
    cutoff_time = datetime.now() - timedelta(minutes=LOOKBACK_MINUTES)
    print(f"[CONFIG] Only processing calls from {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')} onwards "
          f"(last {LOOKBACK_MINUTES} minutes).")

    processed = load_processed()
    print(f"=== auto_process_calls started. "
          f"{len(processed)} calls already processed previously. ===")
    session = requests.Session()
    while True:
        print("[AUTO_POLLER][AUTH] Logging in to CTI panel...")
        try:
            if login(session):
                print("[AUTO_POLLER][AUTH] Login successful.")
                break
            print("[AUTO_POLLER][AUTH] Login failed... Retrying in {POLL_INTERVAL_SECONDS}s...")
        except Exception:
            print("[AUTO_POLLER][ERROR] Exception during initial login:")
            traceback.print_exc()
        time.sleep(POLL_INTERVAL_SECONDS)

    while True:
        try:
            calls = fetch_call_rows(session)
            print(f"[POLL] Total answered+recorded calls fetched from page: {len(calls)}")

            def is_recent_enough(c):
                dt = parse_call_datetime(c["call_date"])
                return dt is not None and dt >= cutoff_time

            new_calls = [
                c for c in calls
                if c["recording_url"] not in processed and is_recent_enough(c)
            ]

            if new_calls:
                print(f"[POLL] Found {len(new_calls)} new answered call(s).")
            else:
                print("[POLL] No new calls (all already processed or none found).")

            for call in new_calls:
                # Process strictly one at a time, in order (oldest first).
                ok = submit_to_web_app(call)
                if ok:
                    processed.add(call["recording_url"])
                    save_processed(processed)

        except Exception:
            print("[AUTO_POLLER][ERROR] Unexpected error during poll:")
            traceback.print_exc()

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()