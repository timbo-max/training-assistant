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
_conversation_history = []
_pending_routine = None

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

def create_hevy_routine(routine_data):
    try:
        headers = {
            "api-key": os.environ["HEVY_API_KEY"],
            "Content-Type": "application/json"
        }
        cleaned = json.loads(json.dumps(routine_data))
        for ex in cleaned.get("exercises", []):
            ex.pop("index", None)
            ex.pop("title", None)
            ex.pop("notes", None)
            ex["sets"] = [
                {k: v for k, v in s.items() if k != "index"}
                for s in ex.get("sets", [])
            ]
        response = requests.post(
            "https://api.hevyapp.com/v1/routines",
            headers=headers,
            json={"routine": cleaned}
        )
        if response.status_code in [200, 201]:
            return True, response.json()
        else:
            return False, response.text
    except Exception as e:
        return False, str(e)

def classify_session_type(exercises):
    push_muscles = ["chest", "shoulder", "tricep", "press", "push", "dip", "fly", "pec"]
    pull_muscles = ["row", "pull", "curl", "bicep", "lat", "back", "deadlift", "shrug"]
    leg_muscles  = ["squat", "leg", "lunge", "calf", "glute", "hamstring", "quad", "hip"]

    push_count = 0
    pull_count = 0
    leg_count  = 0

    for ex in exercises:
        name = ex.get("title", "").lower()
        if any(m in name for m in push_muscles):
            push_count += 1
        if any(m in name for m in pull_muscles):
            pull_count += 1
        if any(m in name for m in leg_muscles):
            leg_count += 1

    total = push_count + pull_count + leg_count
    if total == 0:
        return "Full Body"

    dominant = max(push_count, pull_count, leg_count)
    if dominant == push_count and push_count > pull_count and push_count > leg_count:
        return "Push"
    elif dominant == pull_count and pull_count > push_count and pull_count > leg_count:
        return "Pull"
    elif dominant == leg_count and leg_count > push_count and leg_count > pull_count:
        return "Legs"
    else:
        return "Full Body"

def format_routine_for_telegram(routine_data):
    lines = [f"Suggested session: {routine_data['title']}", ""]
    for i, ex in enumerate(routine_data["exercises"]):
        sets = ex.get("sets", [])
        set_lines = []
        for s in sets:
            if s.get("duration_seconds"):
                set_lines.append(f"{s['duration_seconds']}s")
            elif s.get("weight_kg") and s.get("reps"):
                set_lines.append(f"{s['weight_kg']}kg x {s['reps']}")
            elif s.get("reps"):
                set_lines.append(f"{s['reps']} reps")
        sets_str = ", ".join(set_lines)
        lines.append(f"{i+1}. {ex['title']} — {sets_str}")
    lines.append("")
    lines.append("Reply 'yes' to create this routine in Hevy, or 'no' to cancel.")
    return "\n".join(lines)

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

            hevy_id  = workout.get("id")
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
            session_id  = session_row[0]["id"] if session_row else None

            for exercise in workout.get("exercises", []):
                sets = exercise.get("sets", [])

                total_volume = sum(
                    (s.get("weight_kg") or 0) * (s.get("reps") or 0)
                    for s in sets
                )
                max_weight  = max((s.get("weight_kg") or 0) for s in sets) if sets else None
                total_reps  = sum((s.get("reps") or 0) for s in sets)
                total_dur   = sum((s.get("duration_seconds") or 0) for s in sets)

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
                    "total_duration_seconds": total_dur if total_dur > 0 else None,
                }, on_conflict="gym_session_id,exercise_index").execute()

            print(f"Hevy sync complete for workout {hevy_id} on {workout_date}")

    except Exception as e:
        print(f"Hevy sync failed: {e}")

def sync_day(garmin, db, d):
    today_hrv = None
    try:
        sleep = garmin.get_sleep_data(d)
        bb    = garmin.get_body_battery(d)
        stats = garmin.get_stats(d)

        hrv_value = None
        try:
            hrv_data  = garmin.get_hrv_data(d)
            hrv_value = hrv_data.get("hrvSummary", {}).get("lastNight") or \
                        hrv_data.get("hrvSummary", {}).get("weeklyAvg")
        except Exception:
            pass

        rhr_value = stats.get("restingHeartRate")
        if rhr_value is None:
            try:
                rhr_data  = garmin.get_rhr_day(d)
                rhr_value = rhr_data.get("restingHeartRate") or \
                            rhr_data.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value")
            except Exception:
                pass

        sleep_dto   = sleep.get("dailySleepDTO", {})
        deep_hours  = None
        rem_hours   = None
        light_hours = None
        awake_hours = None
        try:
            deep_seconds  = sleep_dto.get("deepSleepSeconds") or 0
            rem_seconds   = sleep_dto.get("remSleepSeconds") or 0
            light_seconds = sleep_dto.get("lightSleepSeconds") or 0
            awake_seconds = sleep_dto.get("awakeSleepSeconds") or 0
            deep_hours    = round(deep_seconds / 3600, 2) if deep_seconds else None
            rem_hours     = round(rem_seconds / 3600, 2) if rem_seconds else None
            light_hours   = round(light_seconds / 3600, 2) if light_seconds else None
            awake_hours   = round(awake_seconds / 3600, 2) if awake_seconds else None
        except Exception:
            pass

        readiness_score = None
        readiness_level = None
        acute_load      = None
        recovery_time   = None
        try:
            readiness = garmin.get_training_readiness(d)
            if readiness:
                latest = readiness[0]
                readiness_score = latest.get("score")
                readiness_level = latest.get("level")
                acute_load      = latest.get("acuteLoad")
                recovery_time   = latest.get("recoveryTime")
        except Exception:
            pass

        db.table("daily_wellness").upsert({
            "date":                       d,
            "sleep_score":                sleep_dto.get("sleepScores", {}).get("overall", {}).get("value"),
            "sleep_hours":                round((sleep_dto.get("sleepTimeSeconds", 0) or 0) / 3600, 2),
            "sleep_deep_hours":           deep_hours,
            "sleep_rem_hours":            rem_hours,
            "sleep_light_hours":          light_hours,
            "sleep_awake_hours":          awake_hours,
            "hrv_rmssd":                  hrv_value,
            "body_battery_start":         bb[0].get("charged") if bb else None,
            "body_battery_end":           bb[-1].get("drained") if bb else None,
            "resting_hr":                 rhr_value,
            "stress_score":               stats.get("averageStressLevel"),
            "steps":                      stats.get("totalSteps"),
            "intensity_minutes_moderate": stats.get("moderateIntensityMinutes"),
            "intensity_minutes_vigorous": stats.get("vigorousIntensityMinutes"),
            "spo2":                       stats.get("averageSpo2"),
            "respiration_rate":           stats.get("avgWakingRespirationValue"),
            "acute_load":                 acute_load,
            "training_readiness_score":   readiness_score,
            "training_readiness_level":   readiness_level,
            "recovery_time_minutes":      recovery_time,
        }, on_conflict="date").execute()

        today_hrv = hrv_value

    except Exception as e:
        print(f"Wellness sync failed for {d}: {e}")

    try:
        activities  = garmin.get_activities_by_date(d, d)
        planned_row = db.table("training_load").select("planned_workout").eq("date", d).execute().data
        planned     = planned_row[0].get("planned_workout") if planned_row else None

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
    db     = get_supabase()
    garmin = get_garmin()
    yesterday = date.today() - timedelta(days=1)
    sync_day(garmin, db, yesterday.isoformat())
    print(f"Garmin sync complete for {yesterday.isoformat()}")

def sync_trainingpeaks():
    db       = get_supabase()
    ical_url = os.environ["TRAININGPEAKS_ICAL_URL"]
    response = requests.get(ical_url)
    cal      = Calendar.from_ical(response.content)
    today    = date.today()
    window_start = today - timedelta(days=7)
    window_end   = today + timedelta(days=7)

    sessions_by_date = {}
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
        text = f"{summary} — {description}".strip(" —")
        if event_date not in sessions_by_date:
            sessions_by_date[event_date] = []
        sessions_by_date[event_date].append(text)

    for event_date, sessions in sessions_by_date.items():
        combined = "\n\n".join(sessions)
        db.table("training_load").upsert({
            "date":            event_date.isoformat(),
            "planned_workout": combined,
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
2. Training load (sessions completed, total distance, total time, gym sessions, acute load trend)
3. Recovery trends (HRV, sleep stages, Body Battery, stress score, training readiness)
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

Keep it under 300 words. Use plain text, no markdown."""}]
    )

    summary = response.content[0].text
    send_telegram_to_me(f"Weekly training summary\n\n{summary}")
    print("Weekly summary sent")
    return "Summary sent", 200

@app.route("/backfill", methods=["GET"])
def backfill():
    if not check_sync_auth():
        return "Unauthorised", 401
    db     = get_supabase()
    garmin = get_garmin()
    today  = date.today()
    start  = today - timedelta(days=90)
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

@app.route("/telegram", methods=["POST"])
def telegram():
    global _conversation_history, _pending_routine
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

    # --- Pending routine confirmation ---
    if _pending_routine:
        if user_msg.lower() in ["yes", "yep", "yeah", "confirm", "looks good", "do it", "ok", "okay", "go ahead"]:
            success, result = create_hevy_routine(_pending_routine)
            if success:
                send_telegram(chat_id, f"Done! {_pending_routine['title']} has been created in Hevy. Open the app and start it when you're ready!")
            else:
                send_telegram(chat_id, f"Something went wrong creating the routine: {result}")
            _pending_routine = None
            return "ok", 200
        elif user_msg.lower() in ["no", "nope", "cancel", "don't", "skip"]:
            _pending_routine = None
            send_telegram(chat_id, "No problem — routine cancelled. Ask me anything else!")
            return "ok", 200

    # --- Commands ---
    if user_msg.lower() == "/clear":
        _conversation_history = []
        _pending_routine = None
        send_telegram(chat_id, "Conversation history cleared!")
        return "ok", 200

    if user_msg.lower() in ["/sync", "/sync today"]:
        try:
            garmin = get_garmin()
            today  = date.today().isoformat()
            sync_day(garmin, db, today)
            sync_trainingpeaks()
            sync_hevy(db, target_date=date.today())
            send_telegram(chat_id, f"Sync complete for {today}!")
        except Exception as e:
            send_telegram(chat_id, f"Sync failed: {e}")
        return "ok", 200

    if user_msg.lower().startswith("/sync "):
        try:
            d      = user_msg.split(" ")[1]
            target = datetime.strptime(d, "%Y-%m-%d").date()
            garmin = get_garmin()
            sync_day(garmin, db, d)
            sync_trainingpeaks()
            sync_hevy(db, target_date=target)
            send_telegram(chat_id, f"Sync complete for {d}!")
        except Exception as e:
            send_telegram(chat_id, f"Sync failed: {e}")
        return "ok", 200

    if user_msg.lower() == "/help":
        help_text = (
            "Available commands:\n\n"
            "/sync — sync today's data\n"
            "/sync YYYY-MM-DD — sync a specific date e.g. /sync 2026-04-15\n"
            "/clear — clear conversation history\n"
            "/help — show this message\n\n"
            "Or just ask me anything about your training!\n\n"
            "To get a gym session suggestion, just ask:\n"
            "'Suggest a gym session for today'"
        )
        send_telegram(chat_id, help_text)
        return "ok", 200

    # --- Normal chat ---
    week_ago      = (date.today() - timedelta(days=7)).isoformat()
    wellness      = db.table("daily_wellness").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    activities    = db.table("activities").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    training      = db.table("training_load").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    gym_sessions  = db.table("gym_sessions").select("*").gte("date", week_ago).order("date", desc=True).execute().data
    gym_exercises = db.table("gym_exercises").select("*").gte("date", week_ago).order("date", desc=True).execute().data

    # Build exercise library from full history
    all_exercises = db.table("gym_exercises").select(
        "exercise_name, exercise_template_id, max_weight_kg, total_reps, sets"
    ).order("date", desc=True).execute().data

    # Deduplicate by template ID keeping most recent
    seen = {}
    for ex in all_exercises:
        tid = ex.get("exercise_template_id")
        if tid and tid not in seen:
            seen[tid] = ex
    exercise_library = list(seen.values())

    is_routine_request = any(phrase in user_msg.lower() for phrase in [
        "suggest a gym", "gym session", "suggest a session", "create a routine",
        "make a routine", "plan a workout", "suggest a workout", "gym workout"
    ])

    if is_routine_request:
        routine_prompt = f"""You are a personal trainer creating a gym session routine.

Today is {date.today().strftime('%d %b %Y')}.

ATHLETE RECOVERY DATA (use this to calibrate intensity):
{json.dumps(wellness[:3] if wellness else [], indent=2, default=str)}

RECENT GYM SESSIONS (avoid repeating same exercises too soon):
{json.dumps(gym_sessions[:5] if gym_sessions else [], indent=2, default=str)}

RECENT GYM EXERCISES WITH WEIGHTS:
{json.dumps(gym_exercises[:30] if gym_exercises else [], indent=2, default=str)}

AVAILABLE EXERCISE LIBRARY (ONLY use exercises from this list — you must use the exact exercise_template_id):
{json.dumps(exercise_library, indent=2, default=str)}

Create a balanced gym session. Rules:
- ONLY use exercises from the AVAILABLE EXERCISE LIBRARY above
- Use the exact exercise_template_id from the library for each exercise
- Base weights on recent performance — progress by 2.5-5kg if last session felt strong, maintain if moderate, reduce if recovery is poor
- Consider today's recovery: HRV, readiness score, body battery, stress
- Avoid exercises done in the last 48 hours if possible
- Classify the session as Push, Pull, Legs, or Full Body based on the exercises chosen
- Include 6-10 exercises with 3-4 sets each
- For weighted exercises include weight_kg and reps
- For timed exercises include duration_seconds

Return ONLY a JSON object in this exact format:
{{
  "session_type": "Push",
  "exercises": [
    {{
      "title": "Incline Chest Press (Machine)",
      "exercise_template_id": "FBF92739",
      "sets": [
        {{"type": "normal", "weight_kg": 37.5, "reps": 8, "duration_seconds": null, "distance_meters": null, "custom_metric": null}},
        {{"type": "normal", "weight_kg": 37.5, "reps": 8, "duration_seconds": null, "distance_meters": null, "custom_metric": null}},
        {{"type": "normal", "weight_kg": 40, "reps": 6, "duration_seconds": null, "distance_meters": null, "custom_metric": null}}
      ]
    }}
  ]
}}

Return only the JSON, nothing else."""

        routine_response = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": routine_prompt}]
        )

        try:
            raw = routine_response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            routine_json = json.loads(raw)

            session_type = routine_json.get("session_type", "Full Body")
            today_str    = date.today().strftime("%d %b %Y")
            title        = f"{session_type} - {today_str}"

            exercises = []
            for i, ex in enumerate(routine_json.get("exercises", [])):
                exercises.append({
                    "index":                i,
                    "title":                ex.get("title"),
                    "notes":                None,
                    "exercise_template_id": ex.get("exercise_template_id"),
                    "superset_id":          None,
                    "rest_seconds":         90,
                    "sets":                 ex.get("sets", []),
                })

            _pending_routine = {
                "title":     title,
                "folder_id": None,
                "exercises": exercises,
            }

            reply = format_routine_for_telegram(_pending_routine)

        except Exception as e:
            print(f"Routine generation failed: {e}")
            reply = "Sorry, I had trouble generating the routine. Try asking again!"
            _pending_routine = None

        send_telegram(chat_id, reply)
        return "ok", 200

    # --- Standard chat ---
    context = f"""You are a personal training assistant. Here is the athlete's data for the last 7 days.

WELLNESS (HRV, sleep stages, Body Battery, resting HR, stress, steps, training readiness, acute load):
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
IMPORTANT FORMATTING RULES — you are responding in Telegram which does not render markdown tables or headers:
- Never use tables under any circumstances
- Never use markdown headers (##, ###)
- Never use bold (**text**)
- Use simple numbered or dash lists instead
- Keep formatting plain and conversational
- For gym sessions use this format: "P1: Broad Jump — 3x5, P2: Pogo Jumps — 3x8" etc.
If data is missing for a day, mention it. Give practical training advice based on recovery trends.
Always consider the planned workout for today when giving advice.
When asked about pace or splits, reference the per km split data directly.
When asked about gym progress, reference weights, volume and reps trends across sessions.
When asked about conditions, reference the weather data if available.
execution_score is Garmin's workout compliance score (0-100).
directWorkoutRpe / perceived_effort is on a 0-100 scale where 70 = 7/10 effort.
Training effect aerobic scale: 0-5 where 5 is highly impacting.
Stamina is percentage remaining at start and end of activity.
training_readiness_score is Garmin's daily readiness out of 100.
training_readiness_level is LOW, MODERATE or HIGH.
acute_load is Garmin's 7-day training load — higher means more recent stress.
recovery_time_minutes is Garmin's estimated time needed before next hard effort.
sleep_deep_hours, sleep_rem_hours, sleep_light_hours show sleep quality breakdown.
stress_score is average daily stress (0-100, lower is better)."""

    _conversation_history.append({"role": "user", "content": f"{context}\n\nAthlete question: {user_msg}"})

    if len(_conversation_history) > 10:
        _conversation_history = _conversation_history[-10:]

    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=_conversation_history
    )

    reply = response.content[0].text
    _conversation_history.append({"role": "assistant", "content": reply})

    if len(reply) <= 4000:
        send_telegram(chat_id, reply)
    else:
        chunks = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
        for chunk in chunks:
            send_telegram(chat_id, chunk)
    return "ok", 200

@app.route("/", methods=["GET"])
def health():
    return "Training assistant is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
