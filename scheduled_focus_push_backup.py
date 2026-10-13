
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import firebase_admin
import requests
import schedule
from dotenv import load_dotenv
from firebase_admin import credentials, firestore


# ---------- Configuration ----------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TIMEZONE = ZoneInfo("Asia/Kolkata")
ACTIVITYWATCH_URL = os.getenv(
    "ACTIVITYWATCH_BASE_URL",
    "http://localhost:5600/api/0",
).rstrip("/")

COLLECTION_NAME = os.getenv(
    "FIREBASE_COLLECTION",
    "dev_focus_metrics",
)

CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH")

ALLOWED_APPS = (
    "code.exe",
    "visual studio code",
    "vs code",
    "windows terminal",
    "powershell",
    "cmd.exe",
    "command prompt",
    "postman",
    "pycharm",
    "chrome",
)

MAX_RETRIES = 5
FALLBACK_FILE = BASE_DIR / "fallback.log"


# ---------- Firebase setup ----------

def initialize_firestore():
    if not CREDENTIALS_PATH:
        raise ValueError(
            "FIREBASE_CREDENTIALS_PATH is missing from .env"
        )

    key_path = Path(CREDENTIALS_PATH)

    if not key_path.is_absolute():
        key_path = BASE_DIR / key_path

    if not key_path.is_file():
        raise FileNotFoundError(
            f"Service-account file not found: {key_path}"
        )

    if not firebase_admin._apps:
        certificate = credentials.Certificate(str(key_path))
        firebase_admin.initialize_app(certificate)

    return firestore.client()


# ---------- Logging and retry ----------

def fallback_log(message, details=None):
    record = {
        "timestamp": datetime.now(TIMEZONE).isoformat(),
        "message": message,
        "details": details,
    }

    with FALLBACK_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")

    print(f"Fallback log: {message}")


def write_with_retry(document_ref, payload):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            document_ref.set(payload)
            return True

        except Exception as error:
            print(
                f"Write attempt {attempt}/{MAX_RETRIES} failed: "
                f"{error}"
            )

            if attempt < MAX_RETRIES:
                delay = 2 ** (attempt - 1)
                print(f"Retrying in {delay} seconds...")
                time.sleep(delay)

    fallback_log(
        "Firestore write failed after all retries",
        payload,
    )
    return False


# ---------- ActivityWatch helpers ----------

def parse_timestamp(value):
    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)

    return parsed.astimezone(TIMEZONE)


def get_json(endpoint, params=None):
    response = requests.get(
        f"{ACTIVITYWATCH_URL}/{endpoint}",
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def find_bucket(bucket_type):
    buckets = get_json("buckets")

    for bucket_id, bucket in buckets.items():
        if bucket.get("type") == bucket_type:
            return bucket_id

    raise RuntimeError(
        f"ActivityWatch bucket not found: {bucket_type}"
    )


def fetch_events(bucket_id, start, end):
    return get_json(
        f"buckets/{bucket_id}/events",
        params={
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )


def reconstruct_intervals(events, period_start, period_end):
    """Build intervals from each event to the next event."""
    ordered = sorted(
        events,
        key=lambda event: parse_timestamp(event["timestamp"]),
    )

    intervals = []

    for index, event in enumerate(ordered):
        event_start = parse_timestamp(event["timestamp"])

        if index + 1 < len(ordered):
            event_end = parse_timestamp(
                ordered[index + 1]["timestamp"]
            )
        else:
            event_end = period_end

        start = max(event_start, period_start)
        end = min(event_end, period_end)

        if start < end:
            intervals.append((start, end, event))

    return intervals


def get_afk_intervals(events, start, end):
    intervals = reconstruct_intervals(events, start, end)

    return [
        (interval_start, interval_end)
        for interval_start, interval_end, event in intervals
        if str(event.get("data", {}).get("status", "")).lower()
        == "afk"
    ]


def subtract_afk(start, end, afk_intervals):
    """Return the seconds not overlapping confirmed AFK."""
    remaining = [(start, end)]

    for afk_start, afk_end in afk_intervals:
        updated = []

        for current_start, current_end in remaining:
            if (
                afk_end <= current_start
                or afk_start >= current_end
            ):
                updated.append((current_start, current_end))
                continue

            if current_start < afk_start:
                updated.append((current_start, afk_start))

            if afk_end < current_end:
                updated.append((afk_end, current_end))

        remaining = updated

    return sum(
        (part_end - part_start).total_seconds()
        for part_start, part_end in remaining
    )


def aggregate_hourly_metrics(start, end):
    window_bucket = find_bucket("currentwindow")
    afk_bucket = find_bucket("afkstatus")

    window_events = fetch_events(window_bucket, start, end)
    afk_events = fetch_events(afk_bucket, start, end)

    afk_intervals = get_afk_intervals(
        afk_events, start, end
    )

    window_intervals = reconstruct_intervals(
        window_events, start, end
    )

    totals = defaultdict(float)

    for interval_start, interval_end, event in window_intervals:
        app_name = str(
            event.get("data", {}).get("app", "")
        ).strip()

        if not app_name:
            continue

        normalized_app = app_name.lower()

        if not any(
            allowed in normalized_app
            for allowed in ALLOWED_APPS
        ):
            continue

        seconds = subtract_afk(
            interval_start,
            interval_end,
            afk_intervals,
        )

        if seconds > 0:
            totals[app_name] += seconds

    return {
        app: round(seconds / 60, 2)
        for app, seconds in totals.items()
        if seconds > 0
    }


# ---------- Hourly Firestore sync ----------

def sync_previous_completed_hour():
    now = datetime.now(TIMEZONE)
    period_end = now.replace(
        minute=0, second=0, microsecond=0
    )
    period_start = period_end - timedelta(hours=1)

    print(
        f"\nProcessing: {period_start.isoformat()} "
        f"to {period_end.isoformat()}"
    )

    try:
        metrics = aggregate_hourly_metrics(
            period_start, period_end
        )

        db = initialize_firestore()
        collection = db.collection(COLLECTION_NAME)

        successful_writes = 0

        for app_name, duration_minutes in metrics.items():
            payload = {
                "date": period_start.date().isoformat(),
                "hour": period_start.hour,
                "app": app_name,
                "duration_minutes": duration_minutes,
            }

            # Stable ID prevents duplicate documents for the
            # same application and hour if a run is repeated.
            safe_app = re.sub(
                r"[^a-zA-Z0-9_-]+", "_", app_name
            ).strip("_")

            document_id = (
                f"{period_start:%Y%m%d_%H}_"
                f"{safe_app}"
            )

            if write_with_retry(
                collection.document(document_id),
                payload,
            ):
                successful_writes += 1
                print(f"Uploaded: {payload}")

        print(
            f"Sync finished. Successful writes: "
            f"{successful_writes}"
        )

        if not metrics:
            print(
                "No allow-listed application time was found "
                "for this hour; no metric documents were written."
            )

    except Exception as error:
        fallback_log(
            "Hourly sync failed",
            {"error": str(error)},
        )


if __name__ == "__main__":
    if "--once" in sys.argv:
        sync_previous_completed_hour()
    else:
        print("Starting hourly focus-metrics scheduler.")
        print("Press Ctrl+C to stop.")

        sync_previous_completed_hour()
        schedule.every(1).hour.do(
            sync_previous_completed_hour
        )

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nScheduler stopped by user.")