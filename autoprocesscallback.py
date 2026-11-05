"""autoprocesscall.py
Poll Enjay Synapse CTI for answered calls with recordings and submit them
sequentially to the local Call Summary app.

Behaviour:
- Collect today's eligible calls from 08:00:00 through 21:30:00 IST.
- 21:30 is the collection cutoff, NOT a processing cutoff.
- Do one final CTI fetch at/after 21:30, then stop collecting calls.
- Process pending calls oldest-first; pending calls may finish after midnight.
- Wait for the off-peak schedule in Schedule.py before each summarization request.
- Keep successful recording URLs in processed_calls.json to avoid reprocessing.

Notes:
- CTI pagination/row limits must be verified. PAGE_SIZE alone cannot guarantee
  that the report returns every call.
- CTI_PASSWORD should be set as a Windows environment variable.
- This module uses a background collector thread and a single sequential worker.
"""

import json
import os
import re
import time
import traceback
import threading
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from queue import PriorityQueue, Empty
from urllib.parse import urljoin, urlencode
from zoneinfo import ZoneInfo

import requests
from Schedule import wait_for_offpeak, today_start_8am


COLLECTION_CUTOFF = dt_time(21, 30, 0)

#CTI_DATE_FORMAT = "%Y-%m-%d"
CTI_DATE_FORMAT = "%d/%m/%Y"
_backfill_done = False

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
CTI_LOGIN_URL = "http://192.168.200.188:6751/newcti/login.php"
CTI_INDEX_URL = "http://192.168.200.188:6751/newcti/index.php"
CTI_BASE_URL = "http://192.168.200.188:6751/newcti/reports/callreports.php"
WEB_APP_SUMMARIZE_URL = "http://127.0.0.1:8080/summarize"

CTI_USERNAME = os.getenv("CTI_USERNAME", "admin")
CTI_PASSWORD = os.getenv("CTI_PASSWORD", "")
CTI_USERNAME_FIELD = "username"
CTI_PASSWORD_FIELD = "pass"

POLL_INTERVAL_SECONDS = 10 * 60
PAGE_SIZE = 500
MAX_RETRIES = 3
SHOW_TRANSCRIPT = True
COLLECTION_CUTOFF = dt_time(21, 30, 0)  # Inclusive: include a call at exactly 21:30:00

#CTI_DATE_FORMAT = "%Y-%m-%d"   # CTI URL madhe jasa dista tasa thev
CTI_DATE_FORMAT = "%d/%m/%Y"
_backfill_done = False         # pahilyanda sagle divas, nantar fakt aaj


ALLOWED_REFERENCES = [
    "45", "52", "13", "12", "17", "10", "15", "43", "39", "58",
    "55", "63", "70", "71", "75", "81", "104", "95", "109", "100",
    "101", "103", "112", "123", "127","126","96"
]

STATE_FILE = Path(__file__).resolve().parent / "processed_calls.json"
DEBUG_HTML_FILE = Path(__file__).resolve().parent / "debug_callreports.html"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Queue entries are (call timestamp, unique tie-breaker, call dictionary).
_pending_queue = PriorityQueue()
_queued_urls = set()
_queued_lock = threading.Lock()
_collector_done = threading.Event()
_stop_collector = threading.Event()


def now_ist():
    return datetime.now(IST)


def parse_call_datetime(call_date_str):
    """Parse CTI date string in YYYY-MM-DD HH:MM:SS format."""
    try:
        return datetime.strptime(call_date_str.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def get_extension(value):
    """Extract leading digits from values such as '101 (Name)' or '+9198...'."""
    if not value:
        return None
    match = re.match(r"^\+?(\d+)", value.strip())
    return match.group(1) if match else None


def load_processed():
    """Return the set of successfully processed recording URLs and last time."""
    if not STATE_FILE.exists():
        return set(), None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            urls = set(data.get("processed_urls", []))
            last_str = data.get("last_processed_time")
            last_dt = parse_call_datetime(last_str) if last_str else None
            return urls, last_dt
        if isinstance(data, list):  # backward compatibility
            return set(data), None
    except Exception:
        print("[STATE][WARN] Could not read processed_calls.json; starting empty.", flush=True)
    return set(), None


def save_processed(processed, last_processed_time=None):
    payload = {
        "processed_urls": sorted(processed),
        "last_processed_time": (
            last_processed_time.strftime("%Y-%m-%d %H:%M:%S")
            if last_processed_time else None
        ),
    }
    temp_path = STATE_FILE.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(STATE_FILE)


def login(session):
    if not CTI_PASSWORD:
        raise RuntimeError(
            "CTI_PASSWORD environment variable is empty. Set it before starting the service."
        )

    session.get(CTI_LOGIN_URL, headers=BROWSER_HEADERS, timeout=30)
    time.sleep(2)
    response = session.post(
        CTI_LOGIN_URL,
        data={
            CTI_USERNAME_FIELD: CTI_USERNAME,
            CTI_PASSWORD_FIELD: CTI_PASSWORD,
            "login": "Login",
        },
        headers={**BROWSER_HEADERS, "Referer": CTI_LOGIN_URL},
        timeout=30,
    )
    response.raise_for_status()

    if 'name="pass"' in response.text.lower() and "location.replace" not in response.text:
        return False

    verify = session.get(CTI_INDEX_URL, headers=BROWSER_HEADERS, timeout=30)
    verify.raise_for_status()
    return "dashboard.php" in verify.text


def is_logged_out(html):
    return 'name="pass"' in html.lower()


# def fetch_call_rows(session, baseline, cutoff_dt):
#     """Fetch report rows, filter to today's 08:00-21:30 window, oldest first.

#     The report's date fields are currently left blank because the exact CTI
#     parameter format has not been verified. Rows not returned by CTI cannot be
#     recovered by local filtering; inspect pagination if returned rows hit cap.
#     """
#     params = {
#         "from": "",
#         "to": "",
#         "seldirection": "",
#         "seldisposition": "answered",
#         "reportof": "",
#         "group": "",
#         "txtnum": "",
#         "numbertype": "",
#         "sid": "",
#         "pagevalue": str(PAGE_SIZE),
#         "fromhr": "00",
#         "frommin": "00",
#         "fromsec": "00",
#         "tohr": "23",
#         "tomin": "59",
#         "tosec": "59",
#     }
#     # url = CTI_BASE_URL + "?" + urlencode(params)
#     # timeout = (10, 240)
#     # response = session.get(url, headers=BROWSER_HEADERS, timeout=timeout)
#     # response.raise_for_status()
#     url = CTI_BASE_URL + "?" + urlencode(params)
#     timeout = (10, 240)
#     print(f"[FETCH] Requesting {day_str} ...", flush=True)
#     t0 = time.time()
#     response = session.get(url, headers=BROWSER_HEADERS, timeout=timeout)
#     print(f"[FETCH] {day_str} responded in {time.time() - t0:.1f}s", flush=True)
#     response.raise_for_status()

#     html = response.text
#     DEBUG_HTML_FILE.write_text(html, encoding="utf-8")

#     if is_logged_out(html):
#         print("[AUTH] Session expired; logging in again.", flush=True)
#         if not login(session):
#             raise RuntimeError("CTI login failed after session expiration.")
#         response = session.get(url, headers=BROWSER_HEADERS, timeout=timeout)
#         response.raise_for_status()
#         html = response.text
#         if is_logged_out(html):
#             raise RuntimeError("Still on CTI login page after re-login.")

#     row_re = re.compile(
#         r'<tr\s+class=["\'](?:even|odd)["\']\s+valign=["\']top["\']>(.*?)</tr>',
#         re.I | re.S,
#     )
#     td_re = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
#     href_re = re.compile(r"""href\s*=\s*["']([^"']+\.wav(?:\?[^"']*)?)["']""", re.I)

#     raw_rows = row_re.findall(html)
#     print(f"[DEBUG] CTI report returned {len(raw_rows)} rows (PAGE_SIZE={PAGE_SIZE}).", flush=True)
#     if len(raw_rows) >= PAGE_SIZE:
#         print(
#             "[WARN] Returned rows reached PAGE_SIZE. Older calls may be missing; "
#             "check CTI pagination/cap.", flush=True
#         )

#     def clean(value):
#         value = re.sub(r"<[^>]+>", " ", value)
#         value = value.replace("&nbsp;", " ").replace("&#39;", "'").replace("&amp;", "&")
#         return re.sub(r"\s+", " ", value).strip()

#     rows = []
#     for row_html in raw_rows:
#         cells = td_re.findall(row_html)
#         if len(cells) < 7:
#             continue

#         call_date = clean(cells[0])
#         call_dt = parse_call_datetime(call_date)
#         if call_dt is None or call_dt < baseline or call_dt > cutoff_dt:
#             continue

#         source = clean(cells[1])
#         destination = clean(cells[2])
#         disposition = clean(cells[3])
#         call_type = clean(cells[4])
#         duration = clean(cells[5])

#         if "ANSWERED" not in disposition.upper():
#             continue

#         recording_match = href_re.search(cells[6])
#         if not recording_match:
#             continue

#         extension = get_extension(destination if call_type.strip().lower() == "incoming" else source)
#         if ALLOWED_REFERENCES and extension not in ALLOWED_REFERENCES:
#             continue

#         rows.append({
#             "call_date": call_date,
#             "source": source,
#             "destination": destination,
#             "disposition": disposition,
#             "type": call_type,
#             "duration": duration,
#             "recording_url": urljoin(url, recording_match.group(1)),
#         })

#     rows.sort(key=lambda row: parse_call_datetime(row["call_date"]) or datetime.max)
#     return rows

# def _fetch_one_day(session, day, baseline, cutoff_dt):
#     day_str = day.strftime(CTI_DATE_FORMAT)

#     params = {
#         "reportof": "isbetween",
#         "from": day_str,
#         "to": day_str,
#         "fromhr": "0",
#         "frommin": "0",
#         "fromsec": "0",
#         "tohr": "23",
#         "tomin": "59",
#         "tosec": "59",
#         "group": "",
#         "seldirection": "",
#         "seldisposition": "answered",
#         "txtnum": "",
#         "btnsub": "Search",
#         "pageoptions": "10",
#         "selsearch": "",
#         "numbertype": "",
#         "sid": "",
#         "pagevalue": str(PAGE_SIZE),
#     }

#     url = CTI_BASE_URL + "?" + urlencode(params)
#     timeout = (10, 90)

#     print(f"\n[FETCH] Requesting date: {day_str}", flush=True)
#     print(f"[FETCH][URL] {url}", flush=True)

#     t0 = time.time()
#     response = session.get(
#         url,
#         headers=BROWSER_HEADERS,
#         timeout=timeout
#     )

#     print(
#         f"[FETCH] {day_str} responded in "
#         f"{time.time() - t0:.1f}s | HTTP {response.status_code}",
#         flush=True
#     )

#     response.raise_for_status()
#     html = response.text
#     DEBUG_HTML_FILE.write_text(html, encoding="utf-8")

#     if is_logged_out(html):
#         print("[AUTH] Session expired; logging in again.", flush=True)

#         if not login(session):
#             raise RuntimeError(
#                 "CTI login failed after session expiration."
#             )

#         response = session.get(
#             url,
#             headers=BROWSER_HEADERS,
#             timeout=timeout
#         )
#         response.raise_for_status()
#         html = response.text

#         if is_logged_out(html):
#             raise RuntimeError(
#                 "Still on CTI login page after re-login."
#             )

#     row_re = re.compile(
#         r'<tr\s+class=["\'](?:even|odd)["\']\s+valign=["\']top["\']>(.*?)</tr>',
#         re.I | re.S,
#     )

#     td_re = re.compile(
#         r"<td\b[^>]*>(.*?)</td>",
#         re.I | re.S
#     )

#     href_re = re.compile(
#         r"""href\s*=\s*["']([^"']+\.wav(?:\?[^"']*)?)["']""",
#         re.I
#     )

#     raw_rows = row_re.findall(html)

#     print(
#         f"[DEBUG] Requested date: {day_str} | "
#         f"Raw rows: {len(raw_rows)} | PAGE_SIZE={PAGE_SIZE}",
#         flush=True
#     )

#     if len(raw_rows) >= PAGE_SIZE:
#         print(
#             f"[WARN] {day_str}: row limit reached; "
#             "some calls may be missing.",
#             flush=True
#         )

#     def clean(value):
#         value = re.sub(r"<[^>]+>", " ", value)
#         value = (
#             value.replace("&nbsp;", " ")
#             .replace("&#39;", "'")
#             .replace("&amp;", "&")
#         )
#         return re.sub(r"\s+", " ", value).strip()

#     rows = []
#     skip_reasons = {
#         "invalid_date": 0,
#         "before_baseline": 0,
#         "before_today_8am": 0,
#         "after_cutoff": 0,
#         "not_answered": 0,
#         "no_recording": 0,
#         "extension_not_allowed": 0,
#         "invalid_columns": 0,
#     }

#     for row_html in raw_rows:
#         cells = td_re.findall(row_html)

#         if len(cells) < 7:
#             skip_reasons["invalid_columns"] += 1
#             continue

#         call_date = clean(cells[0])
#         call_dt = parse_call_datetime(call_date)

#         if call_dt is None:
#             skip_reasons["invalid_date"] += 1
#             continue

#         if call_dt < baseline:
#             skip_reasons["before_baseline"] += 1
#             continue

#         # Aaj sakali 8 chya aadiche calls skip kara.
#         if (
#             call_dt.date() == now_ist().date()
#             and call_dt.time() < dt_time(8, 0)
#         ):
#             skip_reasons["before_today_8am"] += 1
#             continue

#         # Aaj ratri 9:30 nantarche calls skip kara.
#         if (
#             call_dt.date() == cutoff_dt.date()
#             and call_dt > cutoff_dt
#         ):
#             skip_reasons["after_cutoff"] += 1
#             continue

#         source = clean(cells[1])
#         destination = clean(cells[2])
#         disposition = clean(cells[3])
#         call_type = clean(cells[4])
#         duration = clean(cells[5])

#         if "ANSWERED" not in disposition.upper():
#             skip_reasons["not_answered"] += 1
#             continue

#         recording_match = href_re.search(cells[6])

#         if not recording_match:
#             skip_reasons["no_recording"] += 1
#             continue

#         extension = get_extension(
#             destination
#             if call_type.strip().lower() == "incoming"
#             else source
#         )

#         if ALLOWED_REFERENCES and extension not in ALLOWED_REFERENCES:
#             skip_reasons["extension_not_allowed"] += 1
#             continue

#         rows.append({
#             "call_date": call_date,
#             "source": source,
#             "destination": destination,
#             "disposition": disposition,
#             "type": call_type,
#             "duration": duration,
#             "recording_url": urljoin(
#                 url,
#                 recording_match.group(1)
#             ),
#         })

#     # Actual dates returned by the CTI report.
#     actual_dates = sorted({
#         row["call_date"][:10]
#         for row in rows
#     })

#     print(
#         f"[DEBUG] Requested date: {day_str} | "
#         f"Actual eligible call dates: {actual_dates[:15]}",
#         flush=True
#     )

#     print(
#         f"[DEBUG] {day_str} | "
#         f"Raw rows: {len(raw_rows)} | "
#         f"Eligible rows: {len(rows)} | "
#         f"Skipped: {skip_reasons}",
#         flush=True
#     )

#     # if rows and not any(
#     #     row["call_date"].startswith(day_str)
#     #     for row in rows
#     # ):
#     #     print(
#     #         f"[WARN] Requested {day_str}, but no eligible call "
#     #         "belongs to that date. Check CTI date filters.",
#     #         flush=True
#     #     )

#     expected_date = day.strftime("%Y-%m-%d")

#     if rows and not any(
#         row["call_date"].startswith(expected_date)
#         for row in rows
#     ):
#         print(
#             f"[WARN] Requested {day_str}, but eligible calls "
#             f"belong to other dates: {actual_dates}",
#             flush=True
#         )
#     return rows


def _fetch_one_day(
    session, day, baseline, cutoff_dt, processed=None
):
    day_str = day.strftime(CTI_DATE_FORMAT)
    timeout = (10, 90)
    page_size = 10
    all_rows = []

    row_re = re.compile(
        r"""<tr\s+class=["'](?:even|odd)["']\s+valign=["']top["']>(.*?)</tr>""",
        re.I | re.S,
    )

    td_re = re.compile(
        r"<td\b[^>]*>(.*?)</td>",
        re.I | re.S,
    )

    href_re = re.compile(
        r"""href\s*=\s*["']([^"']*\.wav(?:\?[^"']*)?)["']""",
        re.I,
    )

    def clean(value):
        value = re.sub(r"<[^>]+>", " ", value)
        value = (
            value.replace("&nbsp;", " ")
            .replace("&#39;", "'")
            .replace("&amp;", "&")
        )
        return re.sub(r"\s+", " ", value).strip()
        
    seen_sigs = set()
    page = 1

    while True:
        params = {
            "page": str(page),
            "from": day_str,
            "to": day_str,
            "seldirection": "",
            "seldisposition": "answered",
            "reportof": "isbetween",
            "group": "",
            "txtnum": "",
            "numbertype": "",
            "sid": "",
            "pagevalue": str(page_size),
            "fromhr": "00",
            "frommin": "00",
            "fromsec": "00",
            "tohr": "23",
            "tomin": "59",
            "tosec": "59",
        }

        response = session.get(
            CTI_BASE_URL,
            params=params,
            headers=BROWSER_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        html = response.text

        if is_logged_out(html):
            if not login(session):
                raise RuntimeError(
                    "CTI login failed after session expiration."
                )

            response = session.get(
                CTI_BASE_URL,
                params=params,
                headers=BROWSER_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            html = response.text

            if is_logged_out(html):
                raise RuntimeError(
                    "Still on CTI login page after re-login."
                )

        raw_rows = row_re.findall(html)

        if not raw_rows:
            print(f"[FETCH] {day_str} page {page}: no rows.", flush=True)
            break

        sig = raw_rows[0]
        if sig in seen_sigs:
            print(f"[FETCH] {day_str} page {page}: repeated page, stopping.", flush=True)
            break
        seen_sigs.add(sig)

        page_rows = []

        for row_html in raw_rows:
            cells = td_re.findall(row_html)

            if len(cells) < 7:
                continue

            call_date = clean(cells[0])
            call_dt = parse_call_datetime(call_date)

            if call_dt is None or call_dt < baseline:
                continue

            if (
                call_dt.date() == now_ist().date()
                and call_dt.time() < dt_time(8, 0)
            ):
                continue

            if (
                call_dt.date() == cutoff_dt.date()
                and call_dt > cutoff_dt
            ):
                continue

            source = clean(cells[1])
            destination = clean(cells[2])
            disposition = clean(cells[3])
            call_type = clean(cells[4])
            duration = clean(cells[5])

            if "ANSWERED" not in disposition.upper():
                continue

            recording_match = href_re.search(cells[6])

            if not recording_match:
                continue

            extension = get_extension(
                destination
                if call_type.strip().lower() == "incoming"
                else source
            )

            if (
                ALLOWED_REFERENCES
                and extension not in ALLOWED_REFERENCES
            ):
                continue

            call_row = {
                "call_date": call_date,
                "source": source,
                "destination": destination,
                "disposition": disposition,
                "type": call_type,
                "duration": duration,
                "recording_url": urljoin(
                    response.url,
                    recording_match.group(1),
                ),
            }

            all_rows.append(call_row)
            page_rows.append(call_row)

        # Queue each page immediately so processing need not wait
        # for every page of this date to be fetched.
        if processed is not None and page_rows:
            add_calls_to_queue(page_rows, processed)

        if len(raw_rows) < page_size:
            break

        page += 1
        time.sleep(0.3)

    return all_rows


def fetch_call_rows(session, baseline, cutoff_dt, processed=None):
    """First run: fetch each day from baseline to today.
    Later runs: fetch only today's calls."""
    global _backfill_done

    last_day = cutoff_dt.date()
    first_day = last_day if _backfill_done else baseline.date()

    all_rows = []
    day = first_day

    while day <= last_day:
        day_str = day.strftime(CTI_DATE_FORMAT)
        day_success = False
        print(f"[BACKFILL] Fetching {day_str} ...", flush=True)
        for attempt in range(1, 4):
            try:
                day_rows = _fetch_one_day(
                    session, day, baseline, cutoff_dt
                )
                all_rows.extend(day_rows)
                if processed is not None:
                    add_calls_to_queue(day_rows, processed)
                day_success = True
                break
                

            except requests.exceptions.ReadTimeout:
                print(
                    f"[WARN] {day_str} timed out "
                    f"(attempt {attempt}/3).",
                    flush=True
                )

                if attempt < 3:
                    time.sleep(5)

            except requests.exceptions.ConnectionError:
                print(
                    f"[WARN] {day_str} connection error "
                    f"(attempt {attempt}/3).",
                    flush=True
                )

                if attempt < 3:
                    time.sleep(5)

        if not day_success:
            print(
                f"[ERROR] {day_str} fetch failed after 3 attempts. "
                "Will retry this date on the next poll.",
                flush=True
            )
            # Do not mark the backfill complete if a date failed.
            return all_rows

        # Move to the next date after success only.
        day += timedelta(days=1)
        time.sleep(1)

    _backfill_done = True

    all_rows.sort(
        key=lambda r: parse_call_datetime(r["call_date"]) or datetime.max
    )

    print(
        f"[BACKFILL] Fetch completed successfully through {last_day}. "
        f"Total eligible rows collected: {len(all_rows)}",
        flush=True
    )

    return all_rows

def submit_to_web_app(call):
    print(
        f"[PROCESS] {call['call_date']} | {call['source']} -> "
        f"{call['destination']} | {call['duration']} | {call['recording_url']}",
        flush=True,
    )
    response = requests.post(
        WEB_APP_SUMMARIZE_URL,
        data={
            "audio_url": call["recording_url"],
            "show_transcript": "1" if SHOW_TRANSCRIPT else "",
        },
        timeout=(10, 6 * 3600),
    )
    if response.status_code != 200:
        print(
            f"[ERROR] web_app returned HTTP {response.status_code} "
            f"for {call['recording_url']}", flush=True
        )
        return False
    print(f"[DONE] {call['recording_url']}", flush=True)
    return True


def add_calls_to_queue(calls, processed):
    added = 0
    for call in calls:
        url = call["recording_url"]
        with _queued_lock:
            if url in processed or url in _queued_urls:
                continue
            _queued_urls.add(url)
        call_dt = parse_call_datetime(call["call_date"])
        if call_dt is None:
            with _queued_lock:
                _queued_urls.discard(url)
            continue
        _pending_queue.put((call_dt, url, call))
        added += 1
    if added:
        print(f"[QUEUE] Added {added} new call(s). Pending queue size: {_pending_queue.qsize()}", flush=True)


def collector_worker(baseline, cutoff_dt, processed):
    """Collect calls until cutoff, including a final fetch, independent of processing."""
    session = requests.Session()
    try:
        while True:
            try:
                print("[AUTO_POLLER][AUTH] Logging in to CTI panel...", flush=True)
                if login(session):
                    print("[AUTO_POLLER][AUTH] Login successful.", flush=True)
                    break
                print("[AUTO_POLLER][AUTH] Login failed; retrying in 60 seconds.", flush=True)
            except Exception:
                print("[AUTO_POLLER][ERROR] CTI login failed:", flush=True)
                traceback.print_exc()
            if _stop_collector.wait(60):
                return

        while not _stop_collector.is_set():
            now = now_ist()
            final_fetch = now.replace(tzinfo=None) >= cutoff_dt
            fetch_ok = False
            try:
                calls = fetch_call_rows(session, baseline, cutoff_dt, processed)
                print(f"[POLL] Eligible answered + recorded calls: {len(calls)}", flush=True)
                add_calls_to_queue(calls, processed)
                fetch_ok = _backfill_done
            except requests.exceptions.ReadTimeout:
                print("[AUTO_POLLER][WARN] CTI read timeout; will retry.", flush=True)
            except requests.exceptions.ConnectionError:
                print("[AUTO_POLLER][WARN] Cannot connect to CTI; will retry.", flush=True)
            except Exception:
                print("[AUTO_POLLER][ERROR] Unexpected CTI collection error:", flush=True)
                traceback.print_exc()

            if final_fetch:
                if fetch_ok:
                    print("[COLLECTOR] 21:30 cutoff reached; final fetch completed. No more calls will be collected.", flush=True)
                    return
                print("[COLLECTOR] Final fetch incomplete; retrying in 60s.", flush=True)
                if _stop_collector.wait(60):
                    return
                continue

            seconds_to_cutoff = (cutoff_dt - now_ist().replace(tzinfo=None)).total_seconds()
            if seconds_to_cutoff <= 0:
                continue
            _stop_collector.wait(min(POLL_INTERVAL_SECONDS, seconds_to_cutoff))
    finally:
        _collector_done.set()
        session.close()


# def run_forever():
#     processed, last_processed_time = load_processed()
#     # baseline = datetime(2026, 10, 1, 8, 0, 0)
#     # cutoff_dt = now_ist().replace(
#     #     hour=21,
#     #     minute=30,
#     #     second=0,
#     #     microsecond=0,
#     #     tzinfo=None
#     # )
#     # current = now_ist().replace(tzinfo=None)
#     baseline = datetime(2026, 10, 1, 8, 0, 0)

#     now = now_ist()
#     cutoff_dt = now.replace(
#         hour=21,
#         minute=30,
#         second=0,
#         microsecond=0,
#         tzinfo=None
#     )
#     current = now.replace(tzinfo=None)

#     print(f"[CONFIG] Collecting today's calls from {baseline:%Y-%m-%d %H:%M:%S} to {cutoff_dt:%Y-%m-%d %H:%M:%S} IST.", flush=True)
#     print(f"[CONFIG] {len(processed)} recording(s) already marked processed.", flush=True)

#     # If the service starts before 08:00, the scan baseline remains today's 08:00.
#     # If it starts after 21:30, it performs one final collection fetch then drains the queue.
#     if current < baseline:
#         seconds = (baseline - current).total_seconds()
#         print(f"[SCHEDULE] Waiting until 08:00 IST to start collecting ({int(seconds)} seconds).", flush=True)
#         time.sleep(seconds)

#     collector = threading.Thread(
#         target=collector_worker,
#         args=(baseline, cutoff_dt, processed),
#         name="cti-call-collector",
#         daemon=True,
#     )
#     collector.start()

#     newest_done = last_processed_time
#     fail_counts = {}

#     while True:
#         try:
#             try:
#                 call_dt, url, call = _pending_queue.get(timeout=2)
#             except Empty:
#                 if _collector_done.is_set() and _pending_queue.empty():
#                     print("[AUTO_POLLER] Collection is complete and no pending calls remain. Exiting.", flush=True)
#                     break
#                 continue

#             # Wait immediately before starting each call's pipeline. The collector
#             # continues fetching in its own thread, including the final 21:30 fetch.
#             try:
#                 wait_for_offpeak(lambda message: print(message, flush=True))
#                 ok = submit_to_web_app(call)
#             except Exception:
#                 print(f"[AUTO_POLLER][ERROR] Error processing {url}:", flush=True)
#                 traceback.print_exc()
#                 ok = False

#             if ok:
#                 processed.add(url)
#                 if newest_done is None or call_dt > newest_done:
#                     newest_done = call_dt
#                 save_processed(processed, newest_done)
#             else:
#                 fail_counts[url] = fail_counts.get(url, 0) + 1
#                 print(f"[AUTO_POLLER] Call failed ({fail_counts[url]}/{MAX_RETRIES}): {url}", flush=True)
#                 if fail_counts[url] < MAX_RETRIES:
#                     # Retry later without allowing this URL to block older/newer calls.
#                     time.sleep(2)
#                     _pending_queue.put((call_dt, url, call))
#                 else:
#                     print(f"[AUTO_POLLER][GIVE UP] Skipping after {MAX_RETRIES} failed attempts for this service run: {url}", flush=True)
#             with _queued_lock:
#                 _queued_urls.discard(url)
#             _pending_queue.task_done()

#         except KeyboardInterrupt:
#             print("[AUTO_POLLER] Shutdown requested. Stopping collector; current request may finish.", flush=True)
#             _stop_collector.set()
#             break
#         except Exception:
#             print("[AUTO_POLLER][ERROR] Unexpected worker error:", flush=True)
#             traceback.print_exc()
#             time.sleep(5)

#     _stop_collector.set()
#     collector.join(timeout=5)


def seconds_until_next_cycle():
    """0 if inside today's 08:00-21:30 window; else seconds until next 08:00."""
    now = now_ist().replace(tzinfo=None)
    today_8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    today_cutoff = now.replace(hour=21, minute=30, second=0, microsecond=0)
    if now < today_8:
        return (today_8 - now).total_seconds()
    if now < today_cutoff:
        return 0
    return ((today_8 + timedelta(days=1)) - now).total_seconds()


def run_cycle(processed, last_processed_time):
    """One day's collect + drain. Returns True if shutdown was requested."""
    _collector_done.clear()

    #baseline = datetime(2026, 10, 1, 0, 0, 0)  
    if last_processed_time:
        baseline = datetime.combine(
            last_processed_time.date(),
            datetime.min.time()
        )
    else:
        baseline = datetime(2026, 10, 1, 0, 0, 0) 
    now = now_ist().replace(tzinfo=None)
    cutoff_dt = now.replace(hour=21, minute=30, second=0, microsecond=0)

    print(f"[CONFIG] Cycle: {baseline:%Y-%m-%d} -> {cutoff_dt:%Y-%m-%d %H:%M:%S} IST. "
          f"{len(processed)} already processed.", flush=True)

    collector = threading.Thread(
        target=collector_worker,
        args=(baseline, cutoff_dt, processed),
        name="cti-call-collector",
        daemon=True,
    )
    collector.start()

    newest_done = last_processed_time
    fail_counts = {}

    while True:
        try:
            try:
                call_dt, url, call = _pending_queue.get(timeout=2)
            except Empty:
                if _collector_done.is_set() and _pending_queue.empty():
                    print("[AUTO_POLLER] Cycle complete; no pending calls.", flush=True)
                    collector.join(timeout=5)
                    return False
                continue

            try:
                wait_for_offpeak(lambda m: print(m, flush=True))
                ok = submit_to_web_app(call)
            except Exception:
                print(f"[AUTO_POLLER][ERROR] Error processing {url}:", flush=True)
                traceback.print_exc()
                ok = False

            if ok:
                processed.add(url)
                if newest_done is None or call_dt > newest_done:
                    newest_done = call_dt
                save_processed(processed, newest_done)
                with _queued_lock:
                    _queued_urls.discard(url)
            else:
                fail_counts[url] = fail_counts.get(url, 0) + 1
                print(f"[AUTO_POLLER] Call failed ({fail_counts[url]}/{MAX_RETRIES}): {url}", flush=True)
                if fail_counts[url] < MAX_RETRIES:
                    time.sleep(2)
                    _pending_queue.put((call_dt, url, call))
                else:
                    print(f"[AUTO_POLLER][GIVE UP] {url}", flush=True)
                    with _queued_lock:
                        _queued_urls.discard(url)
                
                
            _pending_queue.task_done()

        except KeyboardInterrupt:
            print("[AUTO_POLLER] Shutdown requested.", flush=True)
            _stop_collector.set()
            return True
        except Exception:
            print("[AUTO_POLLER][ERROR] Unexpected worker error:", flush=True)
            traceback.print_exc()
            time.sleep(5)


def run_forever():
    processed, last_processed_time = load_processed()
    first_cycle = True

    while True:
        if not first_cycle:
            wait = seconds_until_next_cycle()
            if wait > 0:
                print(f"[SCHEDULE] Next collection cycle starts in {int(wait)} seconds (08:00 IST).", flush=True)
                if _stop_collector.wait(wait):
                    break
        first_cycle = False

        if run_cycle(processed, last_processed_time):
            break

        _, last_processed_time = load_processed()

    _stop_collector.set()




if __name__ == "__main__":
    run_forever()
