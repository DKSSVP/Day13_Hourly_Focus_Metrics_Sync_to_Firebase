
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

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo("Asia/Kolkata")
AW_URL = os.getenv(
    "ACTIVITYWATCH_BASE_URL",
    "http://localhost:5600/api/0",
).rstrip("/")
COLLECTION = os.getenv("FIREBASE_COLLECTION", "dev_focus_metrics")
CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH")
ALLOWED_APPS = {
    "code.exe": "VS Code",
    "windowsterminal.exe": "Terminal",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "postman.exe": "Postman",
    "pycharm64.exe": "PyCharm",
    "chrome.exe": "Chrome",
}
MAX_RETRIES = 5
FALLBACK_FILE = BASE_DIR / "fallback.log"


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def get_json(endpoint, params=None):
    response = requests.get(
        f"{AW_URL}/{endpoint}", params=params, timeout=20
    )
    response.raise_for_status()
    return response.json()


def find_bucket(bucket_type):
    buckets = get_json("buckets")
    for bucket_id, bucket in buckets.items():
        if bucket.get("type") == bucket_type:
            return bucket_id
    raise RuntimeError(f"ActivityWatch bucket not found: {bucket_type}")


def fetch_events(bucket_id, start, end):
    return get_json(
        f"buckets/{bucket_id}/events",
        {"start": start.isoformat(), "end": end.isoformat()},
    )


def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(start, end, excluded):
    remaining = [(start, end)]
    for ex_start, ex_end in excluded:
        updated = []
        for cur_start, cur_end in remaining:
            if ex_end <= cur_start or ex_start >= cur_end:
                updated.append((cur_start, cur_end))
                continue
            if cur_start < ex_start:
                updated.append((cur_start, ex_start))
            if ex_end < cur_end:
                updated.append((ex_end, cur_end))
        remaining = updated
    return remaining


def aggregate_hourly_metrics(start, end):
    window_bucket = find_bucket("currentwindow")
    afk_bucket = find_bucket("afkstatus")
    window_events = fetch_events(window_bucket, start, end)
    afk_events = fetch_events(afk_bucket, start, end)

    # Match Day 12: AFK intervals use recorded event durations.
    afk_intervals = []
    for event in afk_events:
        if event.get("data", {}).get("status") != "afk":
            continue
        event_start = parse_timestamp(event["timestamp"])
        event_end = event_start + timedelta(
            seconds=max(0, event.get("duration", 0))
        )
        clipped = (max(event_start, start), min(event_end, end))
        if clipped[1] > clipped[0]:
            afk_intervals.append(clipped)
    afk_intervals = merge_intervals(afk_intervals)

    # Match Day 12: each window event lasts until the next event;
    # the final event uses its recorded duration.
    ordered = sorted(
        window_events,
        key=lambda e: parse_timestamp(e["timestamp"]),
    )
    totals = defaultdict(list)

    for index, event in enumerate(ordered):
        event_start = parse_timestamp(event["timestamp"])
        if index + 1 < len(ordered):
            event_end = parse_timestamp(ordered[index + 1]["timestamp"])
        else:
            event_end = event_start + timedelta(
                seconds=max(0, event.get("duration", 0))
            )

        clipped_start = max(event_start, start)
        clipped_end = min(event_end, end)
        if clipped_end <= clipped_start:
            continue

        app_key = str(event.get("data", {}).get("app", "")).lower()
        app_name = ALLOWED_APPS.get(app_key)
        if not app_name:
            continue

        totals[app_name].extend(
            subtract_intervals(clipped_start, clipped_end, afk_intervals)
        )

    # Merge intervals per application to avoid double counting.
    return {
        app: round(
            sum((b - a).total_seconds() for a, b in merge_intervals(parts))
            / 60,
            2,
        )
        for app, parts in totals.items()
        if parts
    }


def initialize_firestore():
    if not CREDENTIALS_PATH:
        raise ValueError("FIREBASE_CREDENTIALS_PATH is missing from .env")
    key_path = Path(CREDENTIALS_PATH)
    if not key_path.is_absolute():
        key_path = BASE_DIR / key_path
    if not key_path.is_file():
        raise FileNotFoundError(f"Service-account file not found: {key_path}")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate(str(key_path))
        )
    return firestore.client()


def fallback_log(message, details=None):
    record = {
        "timestamp": datetime.now(TZ).isoformat(),
        "message": message,
        "details": details,
    }
    with FALLBACK_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")
    print(f"Fallback logged: {message}")


def write_with_retry(document_ref, payload):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            document_ref.set(payload)
            return True
        except Exception as error:
            print(f"Write attempt {attempt}/{MAX_RETRIES} failed: {error}")
            if attempt < MAX_RETRIES:
                delay = 2 ** (attempt - 1)
                print(f"Retrying in {delay} seconds...")
                time.sleep(delay)
    fallback_log("Firestore write failed after retries", payload)
    return False


def sync_hour(period_start, period_end):
    print(f"\nProcessing {period_start.isoformat()} to {period_end.isoformat()}")
    try:
        metrics = aggregate_hourly_metrics(period_start, period_end)
        db = initialize_firestore()
        collection = db.collection(COLLECTION)
        successful = 0

        for app, minutes in metrics.items():
            payload = {
                "date": period_start.date().isoformat(),
                "hour": period_start.hour,
                "app": app,
                "duration_minutes": minutes,
            }
            safe_app = re.sub(r"[^a-zA-Z0-9_-]+", "_", app).strip("_")
            doc_id = f"{period_start:%Y%m%d_%H}_{safe_app}"
            if write_with_retry(collection.document(doc_id), payload):
                successful += 1
                print(f"Uploaded: {payload}")

        if not metrics:
            print("No allow-listed application time found for this hour.")
        print(f"Sync finished. Successful writes: {successful}")
    except Exception as error:
        fallback_log(
            "Hourly sync failed",
            {
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "error": str(error),
            },
        )


def sync_previous_completed_hour():
    end = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    sync_hour(end - timedelta(hours=1), end)


def run_scheduler():
    print("Hourly focus-metrics scheduler started. Press Ctrl+C to stop.")
    # Process the most recently completed hour at startup.
    sync_previous_completed_hour()

    # Align subsequent runs to the start of each clock hour.
    schedule.every().hour.at(":00").do(sync_previous_completed_hour)
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nScheduler stopped.")


if __name__ == "__main__":
    if "--once" in sys.argv:
        sync_previous_completed_hour()
    else:
        run_scheduler()