import os
import json
import requests
from datetime import date, timedelta
from flask import Flask, request
from garminconnect import Garmin
from supabase import create_client
from anthropic import Anthropic
from twilio.twiml.messaging_response import MessagingResponse
from icalendar import Calendar

app = Flask(__name__)

_supabase = None
_anthropic = None
_garmin = None

def get_supabase():
    global _supabase
    if _supabase is None:
        _supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _supabase

def get_anthropic():
    global _anthropic
    if _anthropic is None:
        _anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic

def get_garmin():
    global _garmin
    if _garmin is None:
        _garmin = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
        _garmin.login()
    return _garmin

def extract_splits(garmin, activity_id):
    try:
        splits = garmin.get_activity_splits(activity_id)
        laps = splits.get("lapDTOs", [])
        result = []
        for i, lap in enumerate(laps):
            distance_m = lap.get("distance", 0) or 0
            duration_s = lap.get("duration", 0) or 0
            if distance_m > 0 and duration_s > 0:
                pace_sec_per_km = (duration_s / distance_m) * 1000
                mins = int(pace_sec_per_km // 60)
                secs = int(pace_sec_per_km % 60)
                result.append({
                    "lap":         i + 1,
                    "distance_km": round(distance_m / 1000, 2),
                    "duration_s":  round(duration_s, 0),
                    "pace_min_km": f"{mins}:{secs:02d}",
                    "avg_hr":      int(lap.get("averageHR")) if lap.get("averageHR") else None,
                })
        return result
    except Exception as e:
        print(f"Splits fetch failed for activity {activity_id}: {e}")
        return None

def sync_day(garmin, db, d):
    try:
        sleep = garmin.get_sleep_data(d)
        hrv   = garmin.get_hrv_data(d)
        bb    = garmin.get_body_battery(d)
        rhr   = garmin.get_rhr_day(d)

        db.table("daily_wellness").upsert({
            "date": d,
            "sleep_score":        sleep.get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value"),
            "sleep_hours":        round((sleep.get("dailySleepDTO", {}).get("sleepTimeSeconds", 0) or 0) / 3600, 2),
            "hrv_rmssd":          hrv.get("hrvSummary", {}).get("lastNight"),
            "body_battery_start": bb[0].get("charged") if bb else None,
            "body_battery_end":   bb[-1].get("drained") if bb else None,
            "resting_hr":         rhr.get("restingHeartRate"),
        }, on_conflict="date").execute()
    except Exception as e:
        print(f"Wellness sync failed for {d}: {e}")

    try:
        activities = garmin.get_activities_by_date(d, d)
        for a in activities:
            activity_id = a.get("activityId")
            sport_type  = a.get("activityType", {}).get("typeKey", "")

            splits = None
            if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                splits = extract_splits(garmin, activity_id)

            db.table("activities").upsert({
                "date":               d,
                "name":               a.get("activityName"),
                "sport_type":         sport_type,
                "duration_seconds":   int(a.get("duration", 0) or 0),
                "distance_km":        round((a.get("distance") or 0) / 1000, 2),
                "avg_hr":             int(a.get("averageHR")) if a.get("averageHR") else None,
                "max_hr":             int(a.get("maxHR")) if a.get("maxHR") else None,
                "elevation_gain_m":   float(a.get("elevationGain")) if a.get("elevationGain") else None,
                "garmin_activity_id": str(activity_id),
                "splits":             json.dumps(splits) if splits else None,
            }, on_conflict="garmin_activity_id").execute()
    except Exception as e:
        print(f"Activity sync failed for {d}: {e}")

def sync_garmin():
    db = get_supabase()
    garmin = get_garmin()
    yesterday = date.today() - timedelta(days=1)
    sync_day(garmin, db, yesterday.isoformat())
    print(f"Garmin sync complete for {yesterday.isoformat()}")

def sync_trainingpeaks():
    db = get_supabase()
    ical_url = os.environ["TRAININGPEAKS_ICAL_URL"]

    response = requests.get(ical_url)
    cal = Calendar.from_ical(response.content)

    today = date.today()
    window_start = today - timedelta(days=7)
    window_end   = today + timedelta(days=7)

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        event_date = dtstart.dt
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        if not (window_start <= event_date <= window_end):
            continue
        summary     = str(component.get("SUMMARY", ""))
        description = str(component.get("DESCRIPTION", ""))
        db.table("training_load").upsert({
            "date":            event_date.isoformat(),
            "planned_workout": f"{summary} — {description}".strip(" —"),
        }, on_conflict="date").execute()

    print("TrainingPeaks sync complete")

@app.route("/sync", methods=["GET"])
def trigger_sync():
    sync_garmin()
    sync_trainingpeaks()
    return "Sync done", 200

@app.route("/backfill", methods=["GET"])
def backfill():
    db = get_supabase()
    garmin = get_garmin()
    today = date.today()
    start = today - timedelta(days=90)
    current = start
    results = []

    while current < today:
        d = current.isoformat()
        print(f"Backfilling activities for {d}...")
        try:
            activities = garmin.get_activities_by_date(d, d)
            for a in activities:
                activity_id = a.get("activityId")
                sport_type  = a.get("activityType", {}).get("typeKey", "")

                splits = None
                if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                    splits = extract_splits(garmin, activity_id)

                db.table("activities").upsert({
                    "date":               d,
                    "name":               a.get("activityName"),
                    "sport_type":         sport_type,
                    "duration_seconds":   int(a.get("duration", 0) or 0),
                    "distance_km":        round((a.get("distance") or 0) / 1000, 2),
                    "avg_hr":             int(a.get("averageHR")) if a.get("averageHR") else None,
                    "max_hr":             int(a.get("maxHR")) if a.get("maxHR") else None,
                    "elevation_gain_m":   float(a.get("elevationGain")) if a.get("elevationGain") else None,
                    "garmin_activity_id": str(activity_id),
                    "splits":             json.dumps(splits) if splits else None,
                }, on_conflict="garmin_activity_id").execute()
            if activities:
                results.append(d)
        except Exception as e:
            print(f"Failed for {d}: {e}")
        current += timedelta(days=1)

    return f"Backfill complete — activities imported for {len(results)} days", 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    db = get_supabase()
    ai = get_anthropic()
    user_msg = request.form.get("Body", "")

    week_ago   = (date.today() - timedelta(days=7)).isoformat()
    wellness   = db.table("daily_wellness").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    activities = db.table("activities").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    training   = db.table("training_load").select("*").gte("date", week_ago).order("date", desc=True).execute().data

    context = f"""You are a personal training assistant. Here is the athlete's data for the last 7 days.

WELLNESS (HRV, sleep, Body Battery, resting HR):
{json.dumps(wellness, indent=2, default=str)}

ACTIVITIES completed (runs, rides, etc.) including per km splits:
{json.dumps(activities, indent=2, default=str)}

TRAINING PLAN (planned workouts from coach):
{json.dumps(training, indent=2, default=str)}

Answer the athlete's question using this data. Be concise, specific, and use the actual numbers.
If data is missing for a day, mention it. Give practical training advice based on recovery trends.
Always consider the planned workout for today when giving advice.
When asked about pace or splits, reference the per km split data directly."""

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": f"{context}\n\nAthlete question: {user_msg}"}]
    )

    reply = response.content[0].text
    twiml = MessagingResponse()
    twiml.message(reply)
    return str(twiml), 200, {"Content-Type": "text/xml"}

@app.route("/", methods=["GET"])
def health():
    return "Training assistant is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
