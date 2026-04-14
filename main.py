import os
import json
import requests
from datetime import date, timedelta, datetime
from flask import Flask, request
from garminconnect import Garmin
from supabase import create_client
from anthropic import Anthropic
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

def send_telegram(chat_id, text):
    token = os.environ["TELEGRAM_TOKEN"]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}
    )

def send_telegram_to_me(text):
    chat_id = os.environ["TELEGRAM_USER_ID"]
    send_telegram(chat_id, text)

def check_sync_auth():
    token = request.args.get("token") or request.headers.get("X-Sync-Token")
    return token == os.environ.get("SYNC_SECRET")

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

def extract_weather(garmin, activity_id):
    try:
        details = garmin.get_activity(activity_id)
        weather = details.get("weatherAndAirQuality", {})
        if not weather:
            return None
        return {
            "temp_c":     weather.get("temperature"),
            "humidity":   weather.get("relativeHumidity"),
            "conditions": weather.get("weatherDescriptor"),
            "wind_speed": weather.get("windSpeed"),
            "feels_like": weather.get("apparentTemperature"),
        }
    except Exception as e:
        print(f"Weather fetch failed for activity {activity_id}: {e}")
        return None

def score_compliance(planned, actual_activities):
    if not planned or not actual_activities:
        return None, None
    ai = get_anthropic()
    actual_summary = json.dumps([{
        "name":             a.get("name"),
        "sport_type":       a.get("sport_type"),
        "duration_seconds": a.get("duration_seconds"),
        "distance_km":      a.get("distance_km"),
    } for a in actual_activities], default=str)

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": f"""Compare the planned workout to the actual activities completed.
Planned: {planned}
Actual: {actual_summary}
Return ONLY a JSON object with two fields:
- score: integer 0-100 (100 = perfect compliance)
- notes: one sentence explaining the score
Example: {{"score": 85, "notes": "Distance slightly short but effort and structure matched well."}}
Return only the JSON, nothing else."""}]
    )
    try:
        result = json.loads(response.content[0].text)
        return result.get("score"), result.get("notes")
    except Exception:
        return None, None

def check_hrv_alert(db, today_hrv):
    if not today_hrv:
        return
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    rows = db.table("daily_wellness").select("hrv_rmssd").gte("date", week_ago).execute().data
    values = [r["hrv_rmssd"] for r in rows if r.get("hrv_rmssd")]
    if len(values) < 3:
        return
    avg = sum(values) / len(values)
    drop_pct = ((avg - today_hrv) / avg) * 100
    if drop_pct >= 15:
        msg = (
            f"HRV alert — your HRV today is {today_hrv:.0f}ms, "
            f"which is {drop_pct:.0f}% below your 7 day average of {avg:.0f}ms. "
            f"Consider an easy day or rest."
        )
        send_telegram_to_me(msg)
        print(f"HRV alert sent: {msg}")

def sync_day(garmin, db, d):
    today_hrv = None
    try:
        sleep = garmin.get_sleep_data(d)
        bb    = garmin.get_body_battery(d)

        hrv_value = None
        try:
            hrv_data = garmin.get_hrv_data(d)
            hrv_value = hrv_data.get("hrvSummary", {}).get("lastNight") or \
                        hrv_data.get("hrvSummary", {}).get("weeklyAvg")
        except Exception:
            pass

        rhr_value = None
        try:
            rhr_data = garmin.get_rhr_day(d)
            rhr_value = rhr_data.get("restingHeartRate") or \
                        rhr_data.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value")
        except Exception:
            pass

        if rhr_value is None:
            try:
                stats = garmin.get_stats(d)
                rhr_value = stats.get("restingHeartRate")
            except Exception:
                pass

        db.table("daily_wellness").upsert({
            "date":               d,
            "sleep_score":        sleep.get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value"),
            "sleep_hours":        round((sleep.get("dailySleepDTO", {}).get("sleepTimeSeconds", 0) or 0) / 3600, 2),
            "hrv_rmssd":          hrv_value,
            "body_battery_start": bb[0].get("charged") if bb else None,
            "body_battery_end":   bb[-1].get("drained") if bb else None,
            "resting_hr":         rhr_value,
        }, on_conflict="date").execute()

        today_hrv = hrv_value

    except Exception as e:
        print(f"Wellness sync failed for {d}: {e}")

    try:
        activities = garmin.get_activities_by_date(d, d)

        planned_row = db.table("training_load").select("planned_workout").eq("date", d).execute().data
        planned = planned_row[0].get("planned_workout") if planned_row else None

        for a in activities:
            activity_id = a.get("activityId")
            sport_type  = a.get("activityType", {}).get("typeKey", "")

            splits  = None
            weather = None
            if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                splits  = extract_splits(garmin, activity_id)
                weather = extract_weather(garmin, activity_id)

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
                "weather":            json.dumps(weather) if weather else None,
            }, on_conflict="garmin_activity_id").execute()

        if planned and activities:
            score, notes = score_compliance(planned, activities)
            if score is not None:
                db.table("training_load").update({
                    "workout_completed": True,
                }).eq("date", d).execute()
                for a in activities:
                    db.table("activities").update({
                        "compliance_score": score,
                        "compliance_notes": notes,
                    }).eq("garmin_activity_id", str(a.get("activityId"))).execute()
                print(f"Compliance score for {d}: {score}/100 — {notes}")

    except Exception as e:
        print(f"Activity sync failed for {d}: {e}")

    if today_hrv:
        try:
            check_hrv_alert(db, today_hrv)
        except Exception as e:
            print(f"HRV alert check failed: {e}")

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
    if not check_sync_auth():
        return "Unauthorised", 401
    sync_garmin()
    sync_trainingpeaks()
    return "Sync done", 200

@app.route("/weekly-summary", methods=["GET"])
def weekly_summary():
    if not check_sync_auth():
        return "Unauthorised", 401
    db = get_supabase()
    ai = get_anthropic()

    week_ago   = (date.today() - timedelta(days=7)).isoformat()
    wellness   = db.table("daily_wellness").select("*").gte("date", week_ago).order("date").execute().data
    activities = db.table("activities").select("*").gte("date", week_ago).order("date").execute().data
    training   = db.table("training_load").select("*").gte("date", week_ago).order("date").execute().data

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Write a concise weekly training summary for an athlete. Use the actual numbers.
Structure it as:
1. Week overview (2 sentences)
2. Training load (sessions completed, total distance, total time)
3. Recovery trends (HRV, sleep, Body Battery patterns)
4. Compliance (how well planned vs actual matched)
5. One key insight or recommendation for next week

WELLNESS DATA:
{json.dumps(wellness, indent=2, default=str)}

ACTIVITIES:
{json.dumps(activities, indent=2, default=str)}

TRAINING PLAN:
{json.dumps(training, indent=2, default=str)}

Keep it under 250 words. Use plain text, no markdown."""}]
    )

    summary = response.content[0].text
    send_telegram_to_me(f"Weekly training summary\n\n{summary}")
    print("Weekly summary sent")
    return "Summary sent", 200

@app.route("/backfill", methods=["GET"])
def backfill():
    if not check_sync_auth():
        return "Unauthorised", 401
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
                splits  = None
                weather = None
                if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                    splits  = extract_splits(garmin, activity_id)
                    weather = extract_weather(garmin, activity_id)
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
                    "weather":            json.dumps(weather) if weather else None,
                }, on_conflict="garmin_activity_id").execute()
            if activities:
                results.append(d)
        except Exception as e:
            print(f"Failed for {d}: {e}")
        current += timedelta(days=1)
    return f"Backfill complete — activities imported for {len(results)} days", 200

@app.route("/strava", methods=["GET", "POST"])
def strava():
    if request.method == "GET":
        verify_token = os.environ["STRAVA_VERIFY_TOKEN"]
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return json.dumps({"hub.challenge": challenge}), 200
        return "Forbidden", 403

    if request.method == "POST":
        data        = request.json
        object_type = data.get("object_type")
        aspect_type = data.get("aspect_type")
        if object_type == "activity" and aspect_type == "create":
            event_time = data.get("event_time")
            if event_time:
                from datetime import timezone
                activity_date = datetime.fromtimestamp(event_time, tz=timezone.utc).date()
                d = activity_date.isoformat()
                print(f"Strava activity uploaded for {d} — syncing Garmin...")
                try:
                    db     = get_supabase()
                    garmin = get_garmin()
                    sync_day(garmin, db, d)
                    sync_trainingpeaks()
                    print(f"Strava-triggered sync complete for {d}")
                except Exception as e:
                    print(f"Strava-triggered sync failed: {e}")
        return "ok", 200

@app.route("/telegram", methods=["POST"])
def telegram():
    db = get_supabase()
    ai = get_anthropic()

    data     = request.json
    message  = data.get("message", {})
    chat_id  = message.get("chat", {}).get("id")
    user_msg = message.get("text", "")
    allowed_id = int(os.environ["TELEGRAM_USER_ID"])
    user_id  = message.get("from", {}).get("id")

    if not chat_id or not user_msg:
        return "ok", 200

    if user_id != allowed_id:
        send_telegram(chat_id, "Sorry, you are not authorised to use this bot.")
        return "ok", 200

    week_ago   = (date.today() - timedelta(days=7)).isoformat()
    wellness   = db.table("daily_wellness").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    activities = db.table("activities").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    training   = db.table("training_load").select("*").gte("date", week_ago).order("date", desc=True).execute().data

    context = f"""You are a personal training assistant. Here is the athlete's data for the last 7 days.

WELLNESS (HRV, sleep, Body Battery, resting HR):
{json.dumps(wellness, indent=2, default=str)}

ACTIVITIES completed (runs, rides, etc.) including per km splits and weather:
{json.dumps(activities, indent=2, default=str)}

TRAINING PLAN (planned workouts from coach) with compliance scores:
{json.dumps(training, indent=2, default=str)}

Answer the athlete's question using this data. Be concise, specific, and use the actual numbers.
If data is missing for a day, mention it. Give practical training advice based on recovery trends.
Always consider the planned workout for today when giving advice.
When asked about pace or splits, reference the per km split data directly.
When asked about conditions, reference the weather data captured during the activity.
Compliance scores are out of 100 and reflect how well the actual session matched the plan."""

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": f"{context}\n\nAthlete question: {user_msg}"}]
    )

    reply = response.content[0].text
    send_telegram(chat_id, reply)
    return "ok", 200

@app.route("/", methods=["GET"])
def health():
    return "Training assistant is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
