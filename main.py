import os
import json
from datetime import date, timedelta
from flask import Flask, request
from garminconnect import Garmin
from supabase import create_client
from anthropic import Anthropic
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

_supabase = None
_anthropic = None

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
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    return client

def sync_garmin():
    db = get_supabase()
    garmin = get_garmin()
    today = date.today()
    yesterday = today - timedelta(days=1)
    d = yesterday.isoformat()

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

    activities = garmin.get_activities_by_date(d, d)
    for a in activities:
        db.table("activities").upsert({
            "date":               d,
            "name":               a.get("activityName"),
            "sport_type":         a.get("activityType", {}).get("typeKey"),
            "duration_seconds":   int(a.get("duration", 0)),
            "distance_km":        round((a.get("distance") or 0) / 1000, 2),
            "avg_hr":             a.get("averageHR"),
            "max_hr":             a.get("maxHR"),
            "elevation_gain_m":   a.get("elevationGain"),
            "garmin_activity_id": str(a.get("activityId")),
        }, on_conflict="garmin_activity_id").execute()

    print(f"Sync complete for {d}")

@app.route("/sync", methods=["GET"])
def trigger_sync():
    sync_garmin()
    return "Sync done", 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    db = get_supabase()
    ai = get_anthropic()
    user_msg = request.form.get("Body", "")

    week_ago   = (date.today() - timedelta(days=7)).isoformat()
    wellness   = db.table("daily_wellness").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    activities = db.table("activities").select("*").gte("date", week_ago).order("date", desc=True).execute().data

    context = f"""You are a personal training assistant. Here is the athlete's data for the last 7 days.

WELLNESS (HRV, sleep, Body Battery, resting HR):
{json.dumps(wellness, indent=2, default=str)}

ACTIVITIES (runs, rides, etc.):
{json.dumps(activities, indent=2, default=str)}

Answer the athlete's question using this data. Be concise, specific, and use the actual numbers.
If data is missing for a day, mention it. Give practical training advice based on recovery trends."""

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
