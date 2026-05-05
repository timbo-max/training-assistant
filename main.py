import os
import json
import requests
import time
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

def claude_haiku(prompt):
    ai = get_anthropic()
    response = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

def claude_sonnet(messages, max_tokens=800):
    ai = get_anthropic()
    response = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=messages
    )
    return response

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

        perceived_effort = summary.get("directWorkoutRpe") or summary.get("perceivedExertion")

        return {
            "avg_pace_min_km":           avg_pace,
            "avg_cadence":               summary.get("averageRunCadence") or
                                         summary.get("averageBikingCadenceInRevPerMinute"),
            "training_effect_aerobic":   summary.get("trainingEffect"),
            "training_effect_anaerobic": summary.get("anaerobicTrainingEffect"),
            "exercise_load":             summary.get("activityTrainingLoad"),
            "body_battery_impact":       summary.get("differenceBodyBattery"),
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
    actual_summary = json.dumps([{
        "name":                      a.get("name"),
        "sport_type":                a.get("sport_type"),
        "duration_seconds":          a.get("duration_seconds"),
        "moving_time_seconds":       a.get("moving_time_seconds"),
        "distance_km":               a.get("distance_km"),
        "avg_hr":                    a.get("avg_hr"),
        "max_hr":                    a.get("max_hr"),
        "avg_pace_min_km":           a.get("avg_pace_min_km"),
        "training_effect_aerobic":   a.get("training_effect_aerobic"),
        "exercise_load":             a.get("exercise_load"),
        "perceived_effort":          a.get("perceived_effort"),
        "stamina_start":             a.get("stamina_start"),
        "stamina_end":               a.get("stamina_end"),
        "splits":                    a.get("splits"),
    } for a in actual_activities], default=str)

    prompt = (
        "You are a running coach analysing workout compliance.\n\n"
        f"PLANNED WORKOUT:\n{planned}\n\n"
        f"ACTUAL WORKOUT DATA:\n{actual_summary}\n\n"
        "Compare planned vs actual. Consider duration, distance, pace zones, interval structure, HR, training effect, perceived effort, stamina.\n\n"
        "Return ONLY a JSON object:\n"
        "{\"score\": <integer 0-100>, \"notes\": \"<two sentences: what matched and what didn't>\"}\n\n"
        "100 = perfect compliance, 0 = completely missed. Return only the JSON, nothing else."
    )
    ai = get_anthropic()
    response = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        result = json.loads(response.content[0].text.strip())
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

def get_hevy_exercise_library():
    try:
        headers = {"api-key": os.environ["HEVY_API_KEY"]}
        all_exercises = []
        page = 1
        while True:
            response = requests.get(
                f"https://api.hevyapp.com/v1/exercise_templates?page={page}&pageSize=100",
                headers=headers
            )
            data = response.json()
            exercises = data.get("exercise_templates", [])
            if not exercises:
                break
            all_exercises.extend(exercises)
            if page >= data.get("page_count", 1):
                break
            page += 1
        return all_exercises
    except Exception as e:
        print(f"Hevy exercise library fetch failed: {e}")
        return []

def refresh_hevy_cache(db):
    try:
        exercises = get_hevy_exercise_library()
        for ex in exercises:
            db.table("hevy_exercise_cache").upsert({
                "exercise_template_id": ex.get("id"),
                "title":                ex.get("title"),
                "synced_at":            datetime.now().isoformat(),
            }, on_conflict="exercise_template_id").execute()
        print(f"Hevy cache refreshed — {len(exercises)} exercises stored")
        return len(exercises)
    except Exception as e:
        print(f"Hevy cache refresh failed: {e}")
        return 0

def create_hevy_stretch_exercises(db):
    try:
        headers = {
            "api-key": os.environ["HEVY_API_KEY"],
            "Content-Type": "application/json"
        }

        # Get all stretches from Supabase
        stretches = db.table("stretch_exercises").select("*").execute().data

        created  = []
        skipped  = []
        failed   = []

        # Get existing Hevy cache to avoid duplicates
        existing = db.table("hevy_exercise_cache").select("title").execute().data
        existing_titles = {e["title"].lower() for e in existing}

        for stretch in stretches:
            name = stretch.get("name", "").strip()
            if not name:
                continue

            # Skip if already in Hevy cache
            if name.lower() in existing_titles:
                skipped.append(name)
                continue

            try:
                response = requests.post(
                    "https://api.hevyapp.com/v1/custom_exercise_templates",
                    headers=headers,
                    json={
                        "exercise_template": {
                            "title":       name,
                            "type":        "duration",
                            "primary_muscle_group":   stretch.get("muscle", "other"),
                            "secondary_muscle_groups": [],
                            "is_custom":   True,
                        }
                    }
                )

                if response.status_code in [200, 201]:
                    data = response.json()
                    template_id = (
                        data.get("exercise_template", {}).get("id") or
                        data.get("id")
                    )
                    if template_id:
                        # Store in hevy_exercise_cache immediately
                        db.table("hevy_exercise_cache").upsert({
                            "exercise_template_id": template_id,
                            "title":                name,
                            "synced_at":            datetime.now().isoformat(),
                        }, on_conflict="exercise_template_id").execute()
                        created.append(name)
                    else:
                        failed.append(name)
                else:
                    print(f"Failed to create {name}: {response.status_code} {response.text}")
                    failed.append(name)

                time.sleep(0.3)  # avoid rate limiting

            except Exception as e:
                print(f"Error creating {name}: {e}")
                failed.append(name)

        print(f"Stretch exercises — created: {len(created)}, skipped: {len(skipped)}, failed: {len(failed)}")
        return created, skipped, failed

    except Exception as e:
        print(f"create_hevy_stretch_exercises failed: {e}")
        return [], [], []
        
def get_cached_exercise_library(db):
    try:
        rows = db.table("hevy_exercise_cache").select(
            "exercise_template_id, title"
        ).order("title").execute().data
        return [{"title": r["title"], "exercise_template_id": r["exercise_template_id"]} for r in rows]
    except Exception as e:
        print(f"Hevy cache read failed: {e}")
        return []

def import_stretch_library(db):
    ninjas_key = os.environ.get("NINJAS_API_KEY")
    if not ninjas_key:
        return 0, "NINJAS_API_KEY not set"

    headers = {"X-Api-Key": ninjas_key}

    # Muscles to fetch stretches for
    muscles = [
        "hamstrings", "quadriceps", "calves", "glutes", "hip_flexors",
        "lower_back", "upper_back", "chest", "shoulders", "triceps",
        "biceps", "forearms", "neck", "abductors", "adductors", "traps"
    ]

    # Context tags by muscle — which routine types benefit from each muscle
    muscle_context_map = {
        "hamstrings":  ["post_run", "post_run_stretch", "mobility", "general"],
        "quadriceps":  ["post_run", "post_run_stretch", "mobility", "general"],
        "calves":      ["post_run", "post_run_stretch", "pre_run_stretch", "mobility", "general"],
        "glutes":      ["post_run", "post_run_stretch", "pre_run_stretch", "mobility", "general"],
        "hip_flexors": ["post_run", "post_run_stretch", "pre_run_stretch", "mobility", "general"],
        "lower_back":  ["post_run", "post_run_stretch", "mobility", "general"],
        "upper_back":  ["mobility", "general"],
        "chest":       ["pre_run_stretch", "mobility", "general"],
        "shoulders":   ["pre_run_stretch", "mobility", "general"],
        "triceps":     ["mobility", "general"],
        "biceps":      ["mobility", "general"],
        "forearms":    ["mobility", "general"],
        "neck":        ["mobility", "general"],
        "abductors":   ["post_run", "post_run_stretch", "pre_run_stretch", "mobility", "general"],
        "adductors":   ["post_run", "post_run_stretch", "mobility", "general"],
        "traps":       ["mobility", "general"],
    }

    all_stretches = {}

    for muscle in muscles:
        try:
            response = requests.get(
                "https://api.api-ninjas.com/v1/exercises",
                headers=headers,
                params={"type": "stretching", "muscle": muscle, "limit": 20}
            )
            if response.status_code == 200:
                exercises = response.json()
                for ex in exercises:
                    name = ex.get("name", "").strip()
                    if name and name not in all_stretches:
                        all_stretches[name] = {
                            "name":             name,
                            "muscle":           ex.get("muscle", muscle),
                            "difficulty":       ex.get("difficulty"),
                            "instructions":     ex.get("instructions"),
                            "suitable_for":     muscle_context_map.get(muscle, ["general"]),
                            "duration_seconds": 30,
                            "bilateral":        True,
                        }
                print(f"Fetched {len(exercises)} stretches for {muscle}")
            else:
                print(f"API Ninjas error for {muscle}: {response.status_code}")
            time.sleep(0.3)
        except Exception as e:
            print(f"Failed to fetch stretches for {muscle}: {e}")

    count = 0
    for name, stretch in all_stretches.items():
        try:
            db.table("stretch_exercises").upsert(
                stretch, on_conflict="name"
            ).execute()
            count += 1
        except Exception as e:
            print(f"Failed to insert stretch {name}: {e}")

    print(f"Stretch library import complete — {count} exercises stored")
    return count, None

def build_stretch_routine(db, context_type, duration_minutes, user_msg):
    try:
        # Duration per exercise varies by type
        if context_type == "pre_run_stretch":
            seconds_per_side = 15
        elif context_type == "post_run_stretch":
            # Check today's session load to decide hold duration
            today     = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            recent_runs = db.table("activities").select(
                "distance_km, exercise_load, sport_type"
            ).in_("sport_type", ["running", "trail_running"]).gte(
                "date", yesterday
            ).lte("date", today).order("date", desc=True).execute().data

            hard_session = False
            if recent_runs:
                latest = recent_runs[0]
                km     = latest.get("distance_km") or 0
                load   = latest.get("exercise_load") or 0
                if km >= 15 or load >= 300:
                    hard_session = True

            seconds_per_side = 45 if hard_session else 30
        else:
            seconds_per_side = 30

        # Time per exercise: bilateral = 2 sides + 5s transition
        seconds_per_exercise = (seconds_per_side * 2) + 5
        max_exercises = (duration_minutes * 60) // seconds_per_exercise

        # Fetch suitable stretches from library
        stretches = db.table("stretch_exercises").select("*").execute().data
        suitable  = [s for s in stretches if context_type in (s.get("suitable_for") or [])]

        if not suitable:
            suitable = stretches

        # Get hevy cache for template ID lookup
        hevy_cache = db.table("hevy_exercise_cache").select(
            "exercise_template_id, title"
        ).execute().data
        hevy_map = {e["title"].lower(): e["exercise_template_id"] for e in hevy_cache}

        # Build context-specific selection instructions
        if context_type == "pre_run_stretch":
            selection_instructions = (
                "This is a PRE RUN stretch routine. Rules:\n"
                "- Prioritise dynamic and activation-focused movements\n"
                "- Focus on hip flexors, glutes, calves, ankles, thoracic rotation\n"
                "- Avoid long static holds — keep it moving\n"
                "- Order: start with ankles/calves, move up to hips and glutes, finish with thoracic\n"
                "- Goal is to warm up and activate, not release tension"
            )
        elif context_type == "post_run_stretch":
            if hard_session:
                selection_instructions = (
                    "This is a POST RUN stretch routine after a HARD or LONG session. Rules:\n"
                    "- Heavy lower body focus — calves, hamstrings, hip flexors, quads, glutes are priority\n"
                    "- At least 70% of stretches should target lower body and hips\n"
                    "- Include piriformis/IT band stretch if available\n"
                    "- Add 1-2 lower back stretches\n"
                    "- Order: calves first, then hamstrings, hip flexors, quads, glutes, lower back\n"
                    "- Long static holds — goal is full release after significant load"
                )
            else:
                selection_instructions = (
                    "This is a POST RUN stretch routine after an easy or moderate session. Rules:\n"
                    "- Lower body focus — calves, hamstrings, hip flexors, glutes\n"
                    "- Can include some upper back and shoulder work\n"
                    "- Order: calves, hamstrings, hip flexors, glutes, optional upper body\n"
                    "- Static holds — goal is recovery and maintenance"
                )
        else:
            selection_instructions = (
                "This is a GENERAL MOBILITY routine. Rules:\n"
                "- Balanced full body selection — hips, thoracic spine, shoulders, hamstrings, ankles\n"
                "- Mix of static and dynamic movements\n"
                "- Not running-specific — focus on general joint health and range of motion\n"
                "- Order: start with hips, thoracic, shoulders, then hamstrings, ankles\n"
                "- Goal is general maintenance and flexibility"
            )

        ai = get_anthropic()
        selection_prompt = (
            f"You are selecting stretches for a {duration_minutes} minute {context_type.replace('_', ' ')} routine.\n"
            f"Pick exactly {int(max_exercises)} stretches from the available list.\n\n"
            f"{selection_instructions}\n\n"
            f"AVAILABLE STRETCHES:\n{json.dumps([{'name': s['name'], 'muscle': s['muscle']} for s in suitable], indent=2)}\n\n"
            "Return ONLY a JSON array of stretch names in the order they should be performed.\n"
            "Example: [\"Standing Quad Stretch\", \"Seated Hamstring Stretch\"]\n"
            "Return only the JSON array, nothing else."
        )

        response = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": selection_prompt}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        selected_names = json.loads(raw)

        exercises    = []
        new_exercises = []
        stretch_map  = {s["name"]: s for s in suitable}

        for i, name in enumerate(selected_names[:int(max_exercises)]):
            stretch     = stretch_map.get(name)
            if not stretch:
                continue

            template_id = hevy_map.get(name.lower())
            if not template_id:
                new_exercises.append(name)
                continue  # skip exercises not yet in Hevy

            bilateral = stretch.get("bilateral", True)
            sets = []
            if bilateral:
                sets = [
                    {"type": "normal", "weight_kg": None, "reps": None, "duration_seconds": seconds_per_side, "distance_meters": None, "custom_metric": None},
                    {"type": "normal", "weight_kg": None, "reps": None, "duration_seconds": seconds_per_side, "distance_meters": None, "custom_metric": None},
                ]
            else:
                sets = [
                    {"type": "normal", "weight_kg": None, "reps": None, "duration_seconds": seconds_per_side, "distance_meters": None, "custom_metric": None},
                ]

            exercises.append({
                "index":                i,
                "title":                name,
                "notes":                stretch.get("instructions", "")[:200] if stretch.get("instructions") else None,
                "exercise_template_id": template_id,
                "superset_id":          None,
                "sets":                 sets,
            })

        return exercises, new_exercises

    except Exception as e:
        print(f"Stretch routine build failed: {e}")
        return [], []

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

def detect_session_type(user_msg):
    prompt = (
        "Based on this session request, identify the session type.\n"
        "Return ONLY one of these exact values: Push, Pull, Legs, Upper Body, Full Body, Running Maintenance, Pre Run, Post Run, Pre Run Stretch, Post Run Stretch, Mobility Stretch\n\n"
        f"Request: \"{user_msg}\"\n\n"
        "Rules:\n"
        "- after a run or post run or after long run or after my run = Post Run (gym session)\n"
        "- before a run or pre run or before my run = Pre Run (gym session)\n"
        "- post run stretch or stretch after run or recovery stretch or stretching after = Post Run Stretch\n"
        "- pre run stretch or stretch before run or warm up stretch or stretching before = Pre Run Stretch\n"
        "- mobility or stretching routine or general stretch or flexibility or just stretch = Mobility Stretch\n"
        "- full body or full bosy or any full body variation = Full Body\n"
        "- upper body = Upper Body\n"
        "- running maintenance or plyometrics or plyo = Running Maintenance\n"
        "- legs or leg day or lower body = Legs\n"
        "- push = Push\n"
        "- pull = Pull\n"
        "- If unclear default to Full Body\n\n"
        "Return only the session type, nothing else."
    )
    return claude_haiku(prompt)

def detect_session_date(user_msg):
    prompt = (
        "Extract the intended workout date from this message. "
        f"Today is {date.today().strftime('%A %d %b %Y')}.\n\n"
        f"Message: \"{user_msg}\"\n\n"
        "If the message mentions a specific day (e.g. Monday, tomorrow, next Tuesday), return that date in format DD Mon YYYY.\n"
        "If no specific date is mentioned, return TODAY.\n"
        "Return only the date string or TODAY, nothing else."
    )
    result = claude_haiku(prompt)
    return result if result != "TODAY" else date.today().strftime("%d %b %Y")

def detect_stretch_duration(user_msg):
    prompt = (
        "Extract the requested duration in minutes from this message.\n"
        f"Message: \"{user_msg}\"\n\n"
        "If the message mentions 10 minutes or 10 min, return 10.\n"
        "If the message mentions 20 minutes or 20 min, return 20.\n"
        "If the message mentions 30 minutes or 30 min, return 30.\n"
        "If no duration is mentioned, return 10.\n"
        "Return only the integer number, nothing else."
    )
    result = claude_haiku(prompt)
    try:
        return int(result.strip())
    except Exception:
        return 10

SESSION_TYPE_DESCRIPTIONS = {
    "Push": "Chest, shoulders, triceps. Include pressing movements like chest press, shoulder press, dips, and tricep work. No pulling or leg movements.",
    "Pull": "Back and biceps. Include rowing movements, lat pulldowns, face pulls, curls. No pushing or leg movements.",
    "Legs": "Quads, hamstrings, glutes, calves. Include squats, leg press, lunges, leg curls, calf raises. No upper body.",
    "Upper Body": "Combined push and pull. Mix of chest, shoulder, back, and arm exercises. No legs. Balanced between pushing and pulling movements.",
    "Full Body": "Balanced mix across all muscle groups — upper push, upper pull, and legs. Good for general conditioning days.",
    "Running Maintenance": "Full body session focused on athletic performance and running economy. Include plyometric exercises (broad jumps, pogo jumps, box jumps, bounding), explosive movements, single leg work, and hip/glute strength. Avoid heavy slow lifts that cause excessive DOMS. This session should complement running training not compromise it.",
    "Pre Run": "Low DOMS risk session before a key running session the next day. Avoid plyometrics, heavy squats, heavy deadlifts, or anything that causes significant muscle damage. Focus on activation, light strength, and mobility-friendly movements. Keep volume moderate and avoid failure. Examples: light upper body pressing and pulling, core work, hip activation, light single leg work with minimal eccentric load.",
    "Post Run": "Recovery-friendly strength session after a run. No plyometrics whatsoever. Focus on steady controlled strength work. Can include moderate squats, upper body pressing and pulling, core, and accessory work. Avoid explosive or high-impact movements. Weights should be moderate — not a PR day.",
}

STRETCH_CONTEXT_MAP = {
    "Pre Run Stretch":  "pre_run_stretch",
    "Post Run Stretch": "post_run_stretch",
    "Mobility Stretch": "mobility",
}

def format_routine_for_telegram(routine_data, new_exercises=None):
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
        name = ex.get("title", "")
        is_new = new_exercises and name in new_exercises
        marker = " *" if is_new else ""
        lines.append(f"{i+1}. {name}{marker} — {sets_str}")

    if new_exercises:
        lines.append("")
        lines.append("* Not yet in Hevy — add as a custom exercise before starting.")

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

        acute_load = None
        try:
            readiness = garmin.get_training_readiness(d)
            if readiness:
                acute_load = readiness[0].get("acuteLoad")
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
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today     = date.today().isoformat()
    sync_day(garmin, db, yesterday)
    sync_day(garmin, db, today)
    print(f"Garmin sync complete for {yesterday} and {today}")

def sync_trainingpeaks():
    db       = get_supabase()
    ical_url = os.environ["TRAININGPEAKS_ICAL_URL"]
    response = requests.get(ical_url)
    cal      = Calendar.from_ical(response.content)
    today    = date.today()
    window_start = today - timedelta(days=7)
    window_end   = today + timedelta(days=7)

    valid_dates = set()
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        event_date = dtstart.dt
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        if window_start <= event_date <= window_end:
            valid_dates.add(event_date.isoformat())

    existing = db.table("training_load").select("date, planned_workout").gte(
        "date", window_start.isoformat()
    ).lte("date", window_end.isoformat()).execute().data

    today_iso = date.today().isoformat()
    for row in existing:
        if row["date"] >= today_iso and row["date"] not in valid_dates and row.get("planned_workout"):
            db.table("training_load").update({
                "planned_workout": None,
            }).eq("date", row["date"]).execute()
            print(f"Cleared moved/deleted session from {row['date']}")

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

def build_stats(db):
    try:
        week_ago   = (date.today() - timedelta(days=7)).isoformat()
        activities = db.table("activities").select("*").gte("date", week_ago).execute().data
        wellness   = db.table("daily_wellness").select("date, acute_load, hrv_rmssd").gte("date", week_ago).order("date", desc=True).execute().data
        gym        = db.table("gym_sessions").select("*").gte("date", week_ago).execute().data
        gym_ex     = db.table("gym_exercises").select("total_volume_kg").gte("date", week_ago).execute().data

        runs   = [a for a in activities if a.get("sport_type") in ["running", "trail_running"]]
        rides  = [a for a in activities if a.get("sport_type") in ["cycling", "road_biking"]]

        total_run_km  = round(sum(a.get("distance_km") or 0 for a in runs), 1)
        total_run_sec = sum(a.get("moving_time_seconds") or a.get("duration_seconds") or 0 for a in runs)
        total_run_hrs = f"{int(total_run_sec // 3600)}h {int((total_run_sec % 3600) // 60)}m"

        total_ride_km  = round(sum(a.get("distance_km") or 0 for a in rides), 1)
        total_ride_sec = sum(a.get("moving_time_seconds") or a.get("duration_seconds") or 0 for a in rides)
        total_ride_hrs = f"{int(total_ride_sec // 3600)}h {int((total_ride_sec % 3600) // 60)}m"

        total_gym_vol = round(sum(e.get("total_volume_kg") or 0 for e in gym_ex), 0)

        acute_loads = [w["acute_load"] for w in wellness if w.get("acute_load")]
        current_load = acute_loads[0] if acute_loads else None
        avg_load     = round(sum(acute_loads) / len(acute_loads), 0) if acute_loads else None

        hrv_values = [w["hrv_rmssd"] for w in wellness if w.get("hrv_rmssd")]
        avg_hrv    = round(sum(hrv_values) / len(hrv_values), 0) if hrv_values else None
        latest_hrv = hrv_values[0] if hrv_values else None

        lines = [f"Training stats — last 7 days ({date.today().strftime('%d %b')})", ""]

        if runs:
            lines.append(f"Running: {len(runs)} sessions, {total_run_km}km, {total_run_hrs}")
        if rides:
            lines.append(f"Riding: {len(rides)} sessions, {total_ride_km}km, {total_ride_hrs}")
        if gym:
            lines.append(f"Gym: {len(gym)} sessions, {total_gym_vol:.0f}kg total volume")
        if not runs and not rides and not gym:
            lines.append("No sessions recorded this week.")

        lines.append("")
        if current_load and avg_load:
            trend = "up" if current_load > avg_load else "down"
            lines.append(f"Acute load: {current_load:.0f} (7-day avg {avg_load:.0f}, trending {trend})")
        if latest_hrv and avg_hrv:
            lines.append(f"HRV: {latest_hrv:.0f}ms today (7-day avg {avg_hrv:.0f}ms)")

        return "\n".join(lines)

    except Exception as e:
        print(f"Stats build failed: {e}")
        return "Could not build stats — try again shortly."

def build_progression(db):
    try:
        ninety_days_ago = (date.today() - timedelta(days=90)).isoformat()
        four_weeks_ago  = (date.today() - timedelta(days=28)).isoformat()

        activities = db.table("activities").select(
            "date, distance_km, avg_pace_min_km, sport_type, name"
        ).in_("sport_type", ["running", "trail_running"]).gte(
            "date", ninety_days_ago
        ).order("date").execute().data

        def pace_to_seconds(pace):
            if not pace:
                return None
            try:
                mins, secs = divmod(float(pace) * 60, 60)
                return int(mins) * 60 + int(secs)
            except Exception:
                return None

        distance_buckets = {
            "5k":            (4.8,  5.2),
            "10k":           (9.5,  10.5),
            "Half marathon": (20.5, 21.5),
            "Marathon":      (41.5, 42.5),
        }

        pb_lines = []
        for label, (lo, hi) in distance_buckets.items():
            matches = [
                a for a in activities
                if a.get("distance_km") and lo <= a["distance_km"] <= hi
                and a.get("avg_pace_min_km")
            ]
            if matches:
                best = min(matches, key=lambda a: pace_to_seconds(a["avg_pace_min_km"]))
                pace = best["avg_pace_min_km"]
                mins = int(pace)
                secs = int((pace - mins) * 60)
                pb_lines.append(f"{label}: {mins}:{secs:02d}/km on {best['date']}")

        all_ex = db.table("gym_exercises").select(
            "exercise_name, max_weight_kg, date"
        ).gte("date", ninety_days_ago).order("date").execute().data

        recent_ex = [e for e in all_ex if e["date"] >= four_weeks_ago]
        older_ex  = [e for e in all_ex if e["date"] < four_weeks_ago]

        by_name_recent = {}
        for e in recent_ex:
            name = e.get("exercise_name")
            w    = e.get("max_weight_kg") or 0
            if name and w > 0:
                if name not in by_name_recent or w > by_name_recent[name]:
                    by_name_recent[name] = w

        by_name_older = {}
        for e in older_ex:
            name = e.get("exercise_name")
            w    = e.get("max_weight_kg") or 0
            if name and w > 0:
                if name not in by_name_older or w > by_name_older[name]:
                    by_name_older[name] = w

        gym_lines = []
        for name, recent_weight in by_name_recent.items():
            older_weight = by_name_older.get(name)
            if older_weight and recent_weight > older_weight:
                diff = round(recent_weight - older_weight, 1)
                gym_lines.append(f"{name}: {older_weight}kg → {recent_weight}kg (+{diff}kg)")

        gym_lines.sort(key=lambda x: float(x.split("+")[1].replace("kg)", "").replace("kg", "")), reverse=True)

        lines = [f"Progression — last 90 days ({date.today().strftime('%d %b')})", ""]

        if pb_lines:
            lines.append("Running PBs (best avg pace by distance):")
            lines.extend([f"- {l}" for l in pb_lines])
        else:
            lines.append("No running PBs found in last 90 days.")

        lines.append("")

        if gym_lines:
            lines.append("Gym — exercises getting stronger (last 4 weeks vs before):")
            lines.extend([f"- {l}" for l in gym_lines[:10]])
        else:
            lines.append("No gym progression found yet — keep logging!")

        return "\n".join(lines)

    except Exception as e:
        print(f"Progression build failed: {e}")
        return "Could not build progression — try again shortly."

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

@app.route("/import-stretches", methods=["GET"])
def import_stretches():
    if not check_sync_auth():
        return "Unauthorised", 401
    db = get_supabase()
    count, error = import_stretch_library(db)
    if error:
        return f"Import failed: {error}", 500
    return f"Stretch library imported — {count} exercises stored", 200

@app.route("/create-stretch-exercises", methods=["GET"])
def create_stretch_exercises_route():
    if not check_sync_auth():
        return "Unauthorised", 401
    db = get_supabase()
    created, skipped, failed = create_hevy_stretch_exercises(db)
    return (
        f"Done — created: {len(created)}, "
        f"skipped (already exist): {len(skipped)}, "
        f"failed: {len(failed)}"
    ), 200
    
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

    if user_msg.lower() == "/stats":
        send_telegram(chat_id, build_stats(db))
        return "ok", 200

    if user_msg.lower() == "/progression":
        send_telegram(chat_id, build_progression(db))
        return "ok", 200

    if user_msg.lower() == "/refresh-library":
        send_telegram(chat_id, "Refreshing Hevy exercise library...")
        count = refresh_hevy_cache(db)
        send_telegram(chat_id, f"Done! {count} exercises cached.")
        return "ok", 200

    if user_msg.lower() in ["/sync", "/sync today"]:
        try:
            garmin = get_garmin()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            today     = date.today().isoformat()
            sync_day(garmin, db, yesterday)
            sync_day(garmin, db, today)
            sync_trainingpeaks()
            sync_hevy(db, target_date=date.today())
            send_telegram(chat_id, f"Sync complete for {yesterday} and {today}!")
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
            "/sync — sync the last 24 hours of data\n"
            "/sync YYYY-MM-DD — sync a specific date\n"
            "/stats — this week's training load at a glance\n"
            "/progression — running PBs and gym gains over 90 days\n"
            "/refresh-library — update cached Hevy exercise library\n"
            "/clear — clear conversation history\n"
            "/help — show this message\n\n"
            "Gym session types:\n"
            "- Push, Pull, Legs, Upper Body, Full Body\n"
            "- Running Maintenance, Pre Run, Post Run\n\n"
            "Stretching routines:\n"
            "- Pre Run Stretch, Post Run Stretch, Mobility\n"
            "- Specify duration e.g. 'make me a 20 min post run stretch'\n\n"
            "Or just ask me anything about your training!"
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

    all_exercises = db.table("gym_exercises").select(
        "exercise_name, exercise_template_id, max_weight_kg, total_reps, sets"
    ).order("date", desc=True).execute().data

    seen = {}
    for ex in all_exercises:
        tid = ex.get("exercise_template_id")
        if tid and tid not in seen:
            seen[tid] = ex
    exercise_history = list(seen.values())

    is_routine_request = any(phrase in user_msg.lower() for phrase in [
        "suggest a gym", "gym session", "suggest a session", "create a routine",
        "make a routine", "plan a workout", "suggest a workout", "gym workout",
        "pre run", "post run", "running maintenance", "upper body", "leg day",
        "push session", "pull session", "full body session",
        "stretching routine", "stretch routine", "mobility session",
        "pre run stretch", "post run stretch", "make me a stretch",
        "stretching session", "flexibility routine", "make me a stretching",
        "mobility routine", "10 min stretch", "20 min stretch", "30 min stretch",
        "10 minute stretch", "20 minute stretch", "30 minute stretch",
    ])

    if is_routine_request:
        detected_type = detect_session_type(user_msg)
        session_date  = detect_session_date(user_msg)

        # --- Stretching routine path ---
        if detected_type in STRETCH_CONTEXT_MAP:
            context_type   = STRETCH_CONTEXT_MAP[detected_type]
            duration_mins  = detect_stretch_duration(user_msg)

            send_telegram(chat_id, f"Building your {duration_mins} min {detected_type.lower()} routine...")

            exercises, new_exercises = build_stretch_routine(db, context_type, duration_mins, user_msg)

            if not exercises:
                send_telegram(chat_id, "No stretches found in the library. Run /import-stretches first to populate the stretch library.")
                return "ok", 200

            title = f"{detected_type} {duration_mins}min - {session_date}"

            _pending_routine = {
                "title":     title,
                "folder_id": None,
                "exercises": exercises,
            }

            reply = format_routine_for_telegram(_pending_routine, set(new_exercises) if new_exercises else None)
            send_telegram(chat_id, reply)
            return "ok", 200

        # --- Gym routine path ---
        full_library_summary = get_cached_exercise_library(db)
        if not full_library_summary:
            full_library_summary = [
                {"title": e.get("title"), "exercise_template_id": e.get("id")}
                for e in get_hevy_exercise_library()
                if e.get("title") and e.get("id")
            ]

        session_type_instruction = (
            f"SESSION TYPE REQUESTED: {detected_type}\n"
            f"{SESSION_TYPE_DESCRIPTIONS.get(detected_type, '')}\n"
            f"You MUST classify this session as \"{detected_type}\" in your response."
        )

        routine_prompt = (
            "You are a personal trainer creating a gym session routine.\n\n"
            f"Today is {date.today().strftime('%d %b %Y')}.\n"
            f"This session is planned for: {session_date}\n\n"
            f"{session_type_instruction}\n\n"
            "ATHLETE RECOVERY DATA (use this to calibrate intensity):\n"
            f"{json.dumps(wellness[:3] if wellness else [], indent=2, default=str)}\n\n"
            "RECENT GYM SESSIONS (avoid repeating same exercises too soon):\n"
            f"{json.dumps(gym_sessions[:5] if gym_sessions else [], indent=2, default=str)}\n\n"
            "RECENT GYM EXERCISES WITH WEIGHTS:\n"
            f"{json.dumps(gym_exercises[:30] if gym_exercises else [], indent=2, default=str)}\n\n"
            "EXERCISE HISTORY LIBRARY (exercises done before with known weights):\n"
            f"{json.dumps(exercise_history, indent=2, default=str)}\n\n"
            "FULL HEVY EXERCISE LIBRARY (you may also use these if appropriate — use exact exercise_template_id):\n"
            f"{json.dumps(full_library_summary, indent=2, default=str)}\n\n"
            "Rules:\n"
            "- Prefer exercises from the history library where possible as weights are known\n"
            "- You MAY use exercises from the full library if they suit the session type better\n"
            "- For exercises not in history, suggest a conservative starting weight and flag them\n"
            "- Use the exact exercise_template_id from whichever library the exercise comes from\n"
            "- Base weights on recent performance — progress by 2.5-5kg if last session felt strong\n"
            "- Consider today's recovery: HRV, body battery, stress, acute load\n"
            "- Avoid exercises done in the last 48 hours if possible\n"
            "- Include 6-10 exercises with 3-4 sets each\n"
            "- For weighted exercises use 6-8 reps unless the exercise is better suited to higher reps (e.g. calf raises, face pulls, core work)\n"
            "- Select weights based on history that would make 6-8 reps challenging but achievable\n"
            "- For timed exercises include duration_seconds\n"
            "- For plyometric exercises use reps, no weight_kg needed\n\n"
            "Return ONLY a JSON object in this exact format:\n"
            "{\n"
            f"  \"session_type\": \"{detected_type}\",\n"
            "  \"new_exercises\": [\"Exercise Name 1\", \"Exercise Name 2\"],\n"
            "  \"exercises\": [\n"
            "    {\n"
            "      \"title\": \"Incline Chest Press (Machine)\",\n"
            "      \"exercise_template_id\": \"FBF92739\",\n"
            "      \"sets\": [\n"
            "        {\"type\": \"normal\", \"weight_kg\": 37.5, \"reps\": 8, \"duration_seconds\": null, \"distance_meters\": null, \"custom_metric\": null}\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "The new_exercises array should list names of any exercises not in the history library.\n"
            "Return only the JSON, nothing else."
        )

        routine_response = claude_sonnet(
            [{"role": "user", "content": routine_prompt}],
            max_tokens=2000
        )

        try:
            raw = routine_response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            routine_json = json.loads(raw)

            session_type  = routine_json.get("session_type", detected_type)
            new_exercises = set(routine_json.get("new_exercises", []))
            title         = f"{session_type} - {session_date}"

            exercises = []
            for i, ex in enumerate(routine_json.get("exercises", [])):
                exercises.append({
                    "index":                i,
                    "title":                ex.get("title"),
                    "notes":                None,
                    "exercise_template_id": ex.get("exercise_template_id"),
                    "superset_id":          None,
                    "sets":                 ex.get("sets", []),
                })

            _pending_routine = {
                "title":     title,
                "folder_id": None,
                "exercises": exercises,
            }

            reply = format_routine_for_telegram(_pending_routine, new_exercises if new_exercises else None)

        except Exception as e:
            print(f"Routine generation failed: {e}")
            reply = "Sorry, I had trouble generating the routine. Try asking again!"
            _pending_routine = None

        send_telegram(chat_id, reply)
        return "ok", 200

    # --- Standard chat ---
    context = (
        f"You are a personal training assistant. Today is {date.today().strftime('%A %d %B %Y')}.\n"
        "Here is the athlete's data for the last 7 days.\n\n"
        "WELLNESS (HRV, sleep stages, Body Battery, resting HR, stress, steps, acute load):\n"
        f"{json.dumps(wellness, indent=2, default=str)}\n\n"
        "CARDIO ACTIVITIES (runs, rides, etc.) including splits, weather, training effect, cadence, stamina:\n"
        f"{json.dumps(activities, indent=2, default=str)}\n\n"
        "TRAINING PLAN (planned workouts from coach — only reference dates that have actual data, do not invent or assume sessions for empty dates):\n"
        f"{json.dumps(training, indent=2, default=str)}\n\n"
        "GYM SESSIONS:\n"
        f"{json.dumps(gym_sessions, indent=2, default=str)}\n\n"
        "GYM EXERCISES (sets, reps, weights, volume per exercise):\n"
        f"{json.dumps(gym_exercises, indent=2, default=str)}\n\n"
        "Answer the athlete's question using this data. Be concise, specific, and use the actual numbers.\n"
        "IMPORTANT FORMATTING RULES — you are responding in Telegram which does not render markdown tables or headers:\n"
        "- Never use tables under any circumstances\n"
        "- Never use markdown headers (##, ###)\n"
        "- Never use bold (**text**)\n"
        "- Use simple numbered or dash lists instead\n"
        "- Keep formatting plain and conversational\n"
        "- For gym sessions use this format: P1: Broad Jump — 3x5, P2: Pogo Jumps — 3x8 etc.\n"
        "If data is missing for a day, mention it. Give practical training advice based on recovery trends.\n"
        "Always consider the planned workout for today when giving advice.\n"
        "When asked about pace or splits, reference the per km split data directly.\n"
        "When asked about gym progress, reference weights, volume and reps trends across sessions.\n"
        "When asked about conditions, reference the weather data if available.\n"
        "directWorkoutRpe / perceived_effort is on a 0-100 scale where 70 = 7/10 effort.\n"
        "Training effect aerobic scale: 0-5 where 5 is highly impacting.\n"
        "Stamina is percentage remaining at start and end of activity.\n"
        "acute_load is Garmin's 7-day training load — higher means more recent stress.\n"
        "sleep_deep_hours, sleep_rem_hours, sleep_light_hours show sleep quality breakdown.\n"
        "stress_score is average daily stress (0-100, lower is better)."
    )

    _conversation_history.append({"role": "user", "content": f"{context}\n\nAthlete question: {user_msg}"})

    if len(_conversation_history) > 6:
        _conversation_history = _conversation_history[-6:]

    response = claude_sonnet(_conversation_history, max_tokens=800)

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
