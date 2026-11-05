import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def is_peak_time(now=None):
    """Return True during DeepSeek peak windows, using India time."""
    now = now or datetime.now(IST)

    # Saturday and Sunday are off-peak.
    if now.weekday() >= 5:
        return False

    current = now.timetz().replace(tzinfo=None)

    return (
        dt_time(6, 30) <= current < dt_time(9, 30)
        or dt_time(11, 30) <= current < dt_time(15, 30)
    )

def wait_for_offpeak(log=print, poll_seconds=30):
    """Wait until DeepSeek off-peak starts, showing current time and resume time."""
    announced = False

    while is_peak_time():
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")

        # Weekday Peak windows from Schedule.py
        if now.weekday() < 5:
            if dt_time(6, 30) <= now.time().replace(tzinfo=None) < dt_time(9, 30):
                resume_time = "09:30 AM"
            else:
                resume_time = "03:30 PM"

        if not announced:
            log(
                f"[SCHEDULE] Current time: {now.strftime('%I:%M %p')} IST. "
                f"It is Peak Time.\n"
                f"[SCHEDULE] Call processing will start at {resume_time} IST. "
                f"Please wait until then."
            )
            announced = True

        time.sleep(poll_seconds)

    if announced:
        now = datetime.now(IST)
        log(
            f"[SCHEDULE] Off-Peak started at {now.strftime('%I:%M:%S %p')} IST. "
            f"Call processing is resuming."
        )
        

def today_start_8am():
    """Today at 08:00 IST, as a naive datetime for CTI call-date comparisons."""
    return datetime.now(IST).replace(
        hour=8, minute=0, second=0, microsecond=0, tzinfo=None
    )
