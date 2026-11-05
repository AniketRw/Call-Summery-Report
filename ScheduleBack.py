
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def is_peak_time():
    now = datetime.now(IST)

    # Saturday and Sunday are off-peak
    if now.weekday() >= 5:
        return False

    current = now.time()

    return (
        dt_time(6, 30) <= current < dt_time(9, 30)
        or dt_time(11, 30) <= current < dt_time(15, 30)
    )


def wait_for_offpeak(log=print):
    announced = False

    while is_peak_time():
        if not announced:
            log(
                "[SCHEDULE] Peak time. Processing paused "
                "until off-peak starts (IST)."
            )
            announced = True

        time.sleep(30)

    if announced:
        log("[SCHEDULE] Off-peak started. Processing resumed.")
