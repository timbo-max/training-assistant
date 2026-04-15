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
        weather = garmin.get_activity_weather(activity_id)
        if not weather:
            return None

        def f_to_c(f):
            if f is None:
                return None
            return round((f - 32) * 5 / 9, 1)

        return {
            "temp_c":     f_to_c(weather.get("temp")),
            "humidity":   weather.get("relativeHumidity"),
            "conditions": weather.get("weatherTypeDTO", {}).get("desc"),
            "wind_speed": weather.get("windSpeed"),
            "wind_dir":   weather.get("windDirectionCompassPoint"),
            "feels_like": f_to_c(weather.get("apparentTemp")),
            "station":    weather.get("weatherStationDTO", {}).get("name"),
        }
    except Exception as e:
        print(f"Weather fetch failed for activity {activity_id}: {e}")
        return None

def extract_activity_details(garmin, activity_id):
    try:
        details = garmin.get_activity(activity_id)
        summary = details.get("summaryDTO", {})

        avg_speed = summary.get("averageSpeed")
        avg_pace = None
        if avg_speed and avg_speed > 0:
            pace_sec = 1000 / avg_speed
            avg_pace = round(pace_sec / 60, 2)

        execution_score = None
        try:
            iq = details.get("connectIQMeasurements", [])
            if iq:
                execution_score = float(iq[0].get("value", 0))
        except Exception:
            pass

        direct_compliance = summary.get("directWorkoutComplianceScore")
        if direct_compliance is not None:
            execution_score = float(direct_compliance)

        perceived_effort = summary.get("directWorkoutRpe") or summary.get("perceivedExertion")

        return {
            "avg_pace_min_km":           avg_pace,
            "avg_cadence":               summary.get("averageRunCadence") or
                                         summary.get("averageBikingCadenceInRevPerMinute"),
            "training_effect_aerobic":   summary.get("trainingEffect"),
            "training_effect_anaerobic": summary.get("anaerobicTrainingEffect"),
            "exercise_load":             summary.get("activityTrainingLoad"),
            "body_battery_impact":       summary.get("differenceBodyBattery"),
            "execution_score":           execution_score,
            "perceived_effort":          perceived_effort,
            "stamina_start":             summary.get("beginPotentialStamina"),
            "stamina_end":               summary.get("endPotentialStamina"),
            "moving_time_seconds":       summary.get("movingDuration"),
            "calories":                  summary.get("calories"),
        }
    except Exception as e:
        print(f"Activity details fetch failed for {activity_id}: {e}")
        return {}

def score_compliance(planned, actual_activities):
    if not planned or not actual_activities:
        return None, None
    ai = get_anthropic()
    actual_summary = json.dumps([{
        "name":                      a.get("name"),
        "sport_type":                a.get("sport_type"),
        "duration_seconds":          a.get("duration_seconds"),
        "moving_time_seconds":       a.get("moving_time_seconds"),
        "distance_km":               a.get("distance_km"),
        "avg_hr":                    a.get("avg_hr"),
        "max_hr":                    a.get("max_hr"),
        "avg_pace_min_km":           a.get("avg_pace_min_km"),
        "avg_cadence":               a.get("avg_cadence"),
        "training_effect_aerobic":   a.get("training_effect_aerobic"),
        "training_effect_anaerobic": a.get("training_effect_anaerobic"),
        "exercise_load":             a.get("exercise_load"),
        "execution_score":           a.get("execution_score"),
        "perceived_effort":          a.get("perceived_effort"),
        "stamina_start":             a.get("stamina_start"),
        "stamina_end":               a.get("stamina_end"),
        "splits":                    a.get("splits"),
    } for a in actual_activities], default=str)

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""You are a running coach analysing workout compliance.

PLANNED WORKOUT:
{planned}

ACTUAL WORKOUT DATA:
{actual_summary}

Compare the planned vs actual workout. Consider:
- Total duration and distance vs planned
- Pace achieved vs target pace zones
- Whether interval structure was completed
- Heart rate data as evidence of effort zones
- Training effect as evidence of intensity achieved
- Execution score from Garmin if available (0-100)
- Perceived effort (directWorkoutRpe is 0-100 scale, 70 = 7/10)
- Stamina data

Return ONLY a JSON object:
{{"score": <integer 0-100>, "notes": "<two sentences: what matched and what didn't>"}}

100 = perfect compliance, 0 = completely missed session.
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

def sync_hevy(db, target_date=None):
    try:
        headers = {"api-key": os.environ["HEVY_API_KEY"]}
        response = requests.get(
            "https://api.hevyapp.com/v1/workouts?page=1&pageSize=10",
            headers=headers
        )
        workouts = response.json().get("workouts", [])

        for workout in workouts:
            start_time = workout.get("start_time", "")
            if not start_time:
                continue

            workout_date = datetime.fromisoformat(start_time.replace("Z", "+00:00")).date()

            if target_date and workout_date != target_date:
                continue

            hevy_id = workout.get("id")
            end_time = workout.get("end_time")

            duration = None
            if start_time and end_time:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_dt   = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration = int((end_dt - start_dt).total_seconds())

            db.table("gym_sessions").upsert({
                "hevy_workout_id":  hevy_id,
                "date":             workout_date.isoformat(),
                "title":            workout.get("title"),
                "start_time":       start_time,
                "end_time":         end_time,
                "duration_seconds": duration,
            }, on_conflict="hevy_workout_id").execute()

            session_row = db.table("gym_sessions").select("id").eq("hevy_workout_id", hevy_id).execute().data
            session_id = session_row[0]["id"] if session_row else None

            for exercise in workout.get("exercises", []):
                sets = exercise.get("sets", [])

                total_volume = sum(
                    (s.get("weight_kg") or 0) * (s.get("reps") or 0)
                    for s in sets
                )
                max_weight = max(
                    (s.get("weight_kg") or 0) for s in sets
                ) if sets else None
                total_reps = sum(
                    (s.get("reps") or 0) for s in sets
                )
                total_duration = sum(
                    (s.get("duration_seconds") or 0) for s in sets
                )

                db.table("gym_exercises").upsert({
                    "gym_session_id":         session_id,
                    "hevy_workout_id":        hevy_id,
                    "date":                   workout_date.isoformat(),
                    "exercise_index":         exercise.get("index"),
                    "exercise_name":          exercise.get("title"),
                    "exercise_template_id":   exercise.get("exercise_template_id"),
                    "superset_id":            exercise.get("superset_id"),
                    "sets":                   json.dumps(sets),
                    "total_volume_kg":        round(total_volume, 2) if total_volume else None,
                    "max_weight_kg":          max_weight,
                    "total_reps":             total_reps if total_reps > 0 else None,
                    "total_duration_seconds": total_duration if total_duration > 0 else None,
                }, on_conflict="gym_session_id,exercise_index").execute()

            print(f"Hevy sync complete for workout {hevy_id} on {workout_date}")

    except Exception as e:
        print(f"Hevy sync failed: {e}")

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
            details = {}
            if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                splits  = extract_splits(garmin, activity_id)
                weather = extract_weather(garmin, activity_id)
                details = extract_activity_details(garmin, activity_id)

            db.table("activities").upsert({
                "date":                      d,
                "name":                      a.get("activityName"),
                "sport_type":                sport_type,
                "duration_seconds":          int(a.get("duration", 0) or 0),
                "distance_km":               round((a.get("distance") or 0) / 1000, 2),
                "avg_hr":                    int(a.get("averageHR")) if a.get("averageHR") else None,
                "max_hr":                    int(a.get("maxHR")) if a.get("maxHR") else None,
                "elevation_gain_m":          float(a.get("elevationGain")) if a.get("elevationGain") else None,
                "garmin_activity_id":        str(activity_id),
                "splits":                    json.dumps(splits) if splits else None,
                "weather":                   json.dumps(weather) if weather else None,
                "avg_pace_min_km":           details.get("avg_pace_min_km"),
                "avg_cadence":               details.get("avg_cadence"),
                "training_effect_aerobic":   details.get("training_effect_aerobic"),
                "training_effect_anaerobic": details.get("training_effect_anaerobic"),
                "exercise_load":             details.get("exercise_load"),
                "body_battery_impact":       details.get("body_battery_impact"),
                "execution_score":           details.get("execution_score"),
                "perceived_effort":          details.get("perceived_effort"),
                "stamina_start":             details.get("stamina_start"),
                "stamina_end":               details.get("stamina_end"),
                "moving_time_seconds":       details.get("moving_time_seconds"),
                "calories":                  details.get("calories"),
            }, on_conflict="garmin_activity_id").execute()

        if planned and activities:
            saved = db.table("activities").select("*").eq("date", d).execute().data
            score, notes = score_compliance(planned, saved)
            if score is not None:
                db.table("training_load").update({
                    "workout_completed": True,
                }).eq("date", d).execute()
                for a in saved:
                    db.table("activities").update({
                        "compliance_score": score,
                        "compliance_notes": notes,
                    }).eq("garmin_activity_id", str(a.get("garmin_activity_id"))).execute()
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
    db = get_supabase()
    sync_hevy(db)
    return "Sync done", 200

@app.route("/sync-date", methods=["GET"])
def sync_specific_date():
    if not check_sync_auth():
        return "Unauthorised", 401
    d = request.args.get("date")
    if not d:
        return "Please provide a date parameter e.g. ?date=2026-04-13", 400
    try:
        target = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD e.g. ?date=2026-04-13", 400
    db     = get_supabase()
    garmin = get_garmin()
    sync_day(garmin, db, d)
    sync_trainingpeaks()
    sync_hevy(db, target_date=target)
    return f"Sync done for {d}", 200

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
    gym        = db.table("gym_sessions").select("*").gte("date", week_ago).order("date").execute().data

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Write a concise weekly training summary for an athlete. Use the actual numbers.
Structure it as:
1. Week overview (2 sentences)
2. Training load (sessions completed, total distance, total time, gym sessions)
3. Recovery trends (HRV, sleep, Body Battery patterns)
4. Compliance (how well planned vs actual matched, reference execution scores)
5. One key insight or recommendation for next week

WELLNESS DATA:
{json.dumps(wellness, indent=2, default=str)}

ACTIVITIES:
{json.dumps(activities, indent=2, default=str)}

TRAINING PLAN:
{json.dumps(training, indent=2, default=str)}

GYM SESSIONS:
{json.dumps(gym, indent=2, default=str)}

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
                details = {}
                if sport_type in ["running", "trail_running", "cycling", "road_biking"]:
                    splits  = extract_splits(garmin, activity_id)
                    weather = extract_weather(garmin, activity_id)
                    details = extract_activity_details(garmin, activity_id)
                db.table("activities").upsert({
                    "date":                      d,
                    "name":                      a.get("activityName"),
                    "sport_type":                sport_type,
                    "duration_seconds":          int(a.get("duration", 0) or 0),
                    "distance_km":               round((a.get("distance") or 0) / 1000, 2),
                    "avg_hr":                    int(a.get("averageHR")) if a.get("averageHR") else None,
                    "max_hr":                    int(a.get("maxHR")) if a.get("maxHR") else None,
                    "elevation_gain_m":          float(a.get("elevationGain")) if a.get("elevationGain") else None,
                    "garmin_activity_id":        str(activity_id),
                    "splits":                    json.dumps(splits) if splits else None,
                    "weather":                   json.dumps(weather) if weather else None,
                    "avg_pace_min_km":           details.get("avg_pace_min_km"),
                    "avg_cadence":               details.get("avg_cadence"),
                    "training_effect_aerobic":   details.get("training_effect_aerobic"),
                    "training_effect_anaerobic": details.get("training_effect_anaerobic"),
                    "exercise_load":             details.get("exercise_load"),
                    "body_battery_impact":       details.get("body_battery_impact"),
                    "execution_score":           details.get("execution_score"),
                    "perceived_effort":          details.get("perceived_effort"),
                    "stamina_start":             details.get("stamina_start"),
                    "stamina_end":               details.get("stamina_end"),
                    "moving_time_seconds":       details.get("moving_time_seconds"),
                    "calories":                  details.get("calories"),
                }, on_conflict="garmin_activity_id").execute()
            if activities:
                results.append(d)
        except Exception as e:
            print(f"Failed for {d}: {e}")
        current += timedelta(days=1)
    return f"Backfill complete — activities imported for {len(results)} days", 200

@app.route("/debug-activity", methods=["GET"])
def debug_activity():
    if not check_sync_auth():
        return "Unauthorised", 401
    activity_id = request.args.get("id")
    if not activity_id:
        return "Please provide ?id=your_garmin_activity_id", 400
    garmin = get_garmin()
    details = garmin.get_activity(int(activity_id))
    return json.dumps(details, indent=2, default=str), 200

@app.route("/debug-hevy", methods=["GET"])
def debug_hevy():
    if not check_sync_auth():
        return "Unauthorised", 401
    headers = {"api-key": os.environ["HEVY_API_KEY"]}
    response = requests.get(
        "https://api.hevyapp.com/v1/workouts?page=1&pageSize=5",
        headers=headers
    )
    return json.dumps(response.json(), indent=2, default=str), 200

@app.route("/debug-weather", methods=["GET"])
def debug_weather():
    if not check_sync_auth():
        return "Unauthorised", 401
    activity_id = request.args.get("id")
    if not activity_id:
        return "Please provide ?id=your_garmin_activity_id", 400
    garmin = get_garmin()
    try:
        weather = garmin.get_activity_weather(int(activity_id))
        return json.dumps(weather, indent=2, default=str), 200
    except Exception as e:
        return f"Error: {e}", 200

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
                    sync_hevy(db, target_date=activity_date)
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

    week_ago      = (date.today() - timedelta(days=7)).isoformat()
    wellness      = db.table("daily_wellness").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    activities    = db.table("activities").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    training      = db.table("training_load").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    gym_sessions  = db.table("gym_sessions").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    gym_exercises = db.table("gym_exercises").select("*").gte("date", week_ago).order("date", desc=True).execute().data

    context = f"""You are a personal training assistant. Here is the athlete's data for the last 7 days.

WELLNESS (HRV, sleep, Body Battery, resting HR):
{json.dumps(wellness, indent=2, default=str)}

CARDIO ACTIVITIES (runs, rides, etc.) including splits, weather, training effect, execution score, cadence, stamina:
{json.dumps(activities, indent=2, default=str)}

TRAINING PLAN (planned workouts from coach):
{json.dumps(training, indent=2, default=str)}

GYM SESSIONS:
{json.dumps(gym_sessions, indent=2, default=str)}

GYM EXERCISES (sets, reps, weights, volume per exercise):
{json.dumps(gym_exercises, indent=2, default=str)}

Answer the athlete's question using this data. Be concise, specific, and use the actual numbers.
If data is missing for a day, mention it. Give practical training advice based on recovery trends.
Always consider the planned workout for today when giving advice.
When asked about pace or splits, reference the per km split data directly.
When asked about gym progress, reference weights, volume and reps trends across sessions.
When asked about conditions, reference the weather data if available.
execution_score is Garmin's workout compliance score (0-100).
directWorkoutRpe / perceived_effort is on a 0-100 scale where 70 = 7/10 effort.
Training effect aerobic scale: 0-5 where 5 is highly impacting.
Stamina is percentage remaining at start and end of activity."""

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
