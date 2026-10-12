import json
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ==================================================
# CONFIGURATION
# ==================================================

BASE_URL = "http://localhost:5600/api/0"
IST = timezone(timedelta(hours=5, minutes=30))

# True  = run the existing 10-minute test
# False = analyse the last 24 hours
TEST_MODE = False

ALLOWED_APPS = {
    "code.exe": "VS Code",
    "windowsterminal.exe": "Terminal",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "postman.exe": "Postman",
    "pycharm64.exe": "PyCharm",
    "chrome.exe": "Chrome",
}


# ==================================================
# HELPER FUNCTIONS
# ==================================================

def parse_timestamp(value):
    """Convert an ISO timestamp into a datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_events(bucket_id, start_time, end_time):
    """Retrieve events from an ActivityWatch bucket."""
    url = f"{BASE_URL}/buckets/{bucket_id}/events"

    params = {
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
    }

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def merge_intervals(intervals):
    """Merge overlapping or adjacent intervals."""
    if not intervals:
        return []

    intervals = sorted(intervals)
    merged = [intervals[0]]

    for start, finish in intervals[1:]:
        previous_start, previous_finish = merged[-1]

        if start <= previous_finish:
            merged[-1] = (
                previous_start,
                max(previous_finish, finish),
            )
        else:
            merged.append((start, finish))

    return merged


def subtract_intervals(start, finish, excluded_intervals):
    """Remove AFK intervals from an application interval."""
    remaining = [(start, finish)]

    for excluded_start, excluded_finish in excluded_intervals:
        updated = []

        for segment_start, segment_finish in remaining:
            if (
                excluded_finish <= segment_start
                or excluded_start >= segment_finish
            ):
                updated.append((segment_start, segment_finish))
                continue

            if segment_start < excluded_start:
                updated.append((segment_start, excluded_start))

            if excluded_finish < segment_finish:
                updated.append((excluded_finish, segment_finish))

        remaining = updated

        if not remaining:
            break

    return remaining


# ==================================================
# MAIN PROGRAM
# ==================================================

def main():
    # Select the analysis period.
    if TEST_MODE: 
        start_time = datetime(
            2026, 10, 9, 11, 48, tzinfo=IST
        )
        end_time = datetime(
            2026, 10, 9, 11, 58, tzinfo=IST
        )
    else:
        end_time = datetime.now(IST)
        start_time = end_time - timedelta(hours=24)

    print("Connecting to ActivityWatch...")

    response = requests.get(
        f"{BASE_URL}/buckets",
        timeout=10,
    )
    response.raise_for_status()
    buckets = response.json()

    window_bucket = next(
        (
            bucket_id
            for bucket_id, details in buckets.items()
            if details.get("type") == "currentwindow"
        ),
        None,
    )

    afk_bucket = next(
        (
            bucket_id
            for bucket_id, details in buckets.items()
            if details.get("type") == "afkstatus"
        ),
        None,
    )

    if not window_bucket or not afk_bucket:
        raise RuntimeError(
            "ActivityWatch window or AFK bucket was not found."
        )

    window_events = get_events(
        window_bucket, start_time, end_time
    )
    afk_events = get_events(
        afk_bucket, start_time, end_time
    )

    print("Window bucket:", window_bucket)
    print("AFK bucket:", afk_bucket)
    print("Window events received:", len(window_events))
    print("AFK events received:", len(afk_events))

    # --------------------------------------------------
    # Build AFK intervals
    # --------------------------------------------------

    afk_intervals = []

    for event in afk_events:
        if event.get("data", {}).get("status") != "afk":
            continue

        event_start = parse_timestamp(event["timestamp"])
        event_finish = event_start + timedelta(
            seconds=max(0, event.get("duration", 0))
        )

        clipped_start = max(event_start, start_time)
        clipped_finish = min(event_finish, end_time)

        if clipped_finish > clipped_start:
            afk_intervals.append(
                (clipped_start, clipped_finish)
            )

    afk_intervals = merge_intervals(afk_intervals)

    afk_seconds = sum(
        (finish - start).total_seconds()
        for start, finish in afk_intervals
    )

    # --------------------------------------------------
    # Reconstruct window intervals
    # --------------------------------------------------
    # Sort chronologically. A window event normally remains
    # active until the next window-change event. Using the next
    # timestamp avoids treating small duration discrepancies
    # between consecutive events as automatically idle time.

    sorted_events = sorted(
        window_events,
        key=lambda event: parse_timestamp(event["timestamp"]),
    )

    app_intervals = defaultdict(list)

    for index, event in enumerate(sorted_events):
        event_start = parse_timestamp(event["timestamp"])

        if index + 1 < len(sorted_events):
            event_finish = parse_timestamp(
                sorted_events[index + 1]["timestamp"]
            )
        else:
            # For the final event, use its recorded duration.
            event_finish = event_start + timedelta(
                seconds=max(0, event.get("duration", 0))
            )

        clipped_start = max(event_start, start_time)
        clipped_finish = min(event_finish, end_time)

        if clipped_finish <= clipped_start:
            continue

        data = event.get("data", {})
        app_key = data.get("app", "").lower()

        # Ignore applications outside the allow-list.
        if app_key not in ALLOWED_APPS:
            continue

        # Subtract time when ActivityWatch reports the user AFK.
        usable_segments = subtract_intervals(
            clipped_start,
            clipped_finish,
            afk_intervals,
        )

        app_name = ALLOWED_APPS[app_key]
        app_intervals[app_name].extend(usable_segments)

    # --------------------------------------------------
    # Calculate application-wise focus time
    # --------------------------------------------------

    summary = {}

    for app_name, intervals in app_intervals.items():
        merged = merge_intervals(intervals)

        seconds = sum(
            (finish - start).total_seconds()
            for start, finish in merged
        )

        summary[app_name] = round(seconds / 60, 2)

    total_seconds = sum(
        minutes * 60 for minutes in summary.values()
    )

    period_seconds = (
        end_time - start_time
    ).total_seconds()

    # --------------------------------------------------
    # Build JSON report
    # --------------------------------------------------

    result = {
        "period_start": start_time.isoformat(),
        "period_end": end_time.isoformat(),
        "period_minutes": round(period_seconds / 60, 2),
        "window_events_received": len(window_events),
        "afk_events_received": len(afk_events),
        "confirmed_afk_minutes": round(afk_seconds / 60, 2),
        "developer_applications": summary,
        "total_tracked_application_minutes": round(
            total_seconds / 60, 2
        ),
        "unaccounted_or_excluded_minutes": round(
            max(0, period_seconds - afk_seconds - total_seconds)
            / 60,
            2,
        ),
        "note": (
            "Application time is estimated from foreground-window "
            "events. AFK intervals are excluded. Applications outside "
            "the allow-list and periods without covered events are "
            "not counted as developer-tool time. This does not prove "
            "that all tracked time was productive work."
        ),
    }

    print("\n--- Focus summary ---")
    print(json.dumps(result, indent=2))

    output_file = "focus_summary.json"

    with open(output_file, "w", encoding="utf-8") as file:
        json.dump(result, file, indent=2)

    print(f"\nJSON report saved to {output_file}")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as error:
        print("ActivityWatch API error:", error)
    except (ValueError, KeyError, RuntimeError) as error:
        print("Processing error:", error)
