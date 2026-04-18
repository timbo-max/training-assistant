# Training Assistant

A personal AI-powered training assistant that automatically syncs data from Garmin, Hevy, and TrainingPeaks into a database, then lets you chat with your training data via Telegram — powered by Claude AI.

---

## What it does

- Automatically pulls Garmin wellness data (HRV, sleep stages, Body Battery, resting HR, stress, steps, SpO2, respiration, training readiness, acute load) and activities (runs, rides) including per km splits, weather, training effect, cadence, stamina, execution score, and perceived effort
- Syncs planned sessions from TrainingPeaks via iCal — supports multiple sessions per day
- Pulls gym sessions from Hevy including every exercise, set, reps, and weight
- Scores workout compliance by comparing planned vs actual sessions
- Suggests gym sessions via Telegram and creates them as routines directly in Hevy
- Sends a HRV fatigue alert via Telegram if HRV drops 15% below your 7-day average
- Sends an automated weekly training summary every Sunday at 6pm
- Lets you chat naturally with your training data via a private Telegram bot with 5-exchange conversation memory

---

## Architecture

```
Garmin Fenix → Garmin Connect → Railway backend → Supabase
Hevy app → Hevy API → Railway backend → Supabase
TrainingPeaks → iCal feed → Railway backend → Supabase
cron-job.org → /sync (7:30am daily) → Railway → Supabase
cron-job.org → /weekly-summary (Sunday 6pm) → Telegram
Telegram message → Railway /telegram → Claude AI → Supabase → Telegram reply
Telegram "suggest gym session" → Claude AI → Hevy API (creates routine)
```

### Services used

| Service | Purpose | Cost |
|---|---|---|
| Railway | Hosts the Python backend | Free tier |
| Supabase | PostgreSQL database | Free tier |
| Anthropic API | Claude AI for chat and routine generation | ~$1-2/month |
| Telegram | Chat interface | Free |
| Garmin Connect | Activity and wellness data source | Free (unofficial API) |
| Hevy | Gym workout tracking + routine creation | $3/month PRO required |
| TrainingPeaks | Planned session data via iCal | Existing subscription |
| cron-job.org | Daily and weekly scheduled syncs | Free |

---

## Database tables

| Table | Contents |
|---|---|
| `daily_wellness` | HRV, sleep stages, Body Battery, resting HR, stress, steps, SpO2, respiration, training readiness, acute load |
| `activities` | Runs, rides etc. with splits, weather, training effect, execution score, cadence, stamina, compliance |
| `training_load` | Planned sessions from TrainingPeaks with compliance scoring |
| `gym_sessions` | Hevy workout sessions with duration and title |
| `gym_exercises` | Every exercise with sets, reps, weight, volume, max weight and template ID |

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/sync` | GET | Pulls last 24 hours of Garmin data + TrainingPeaks + recent Hevy sessions |
| `/sync-date?date=YYYY-MM-DD` | GET | Syncs a specific date |
| `/weekly-summary` | GET | Generates and sends weekly summary to Telegram |
| `/backfill` | GET | Imports last 90 days of Garmin activities |
| `/telegram` | POST | Telegram bot webhook |
| `/` | GET | Health check |

All endpoints except `/telegram` require `?token=YOUR_SYNC_SECRET`.

---

## Telegram commands and features

| Command / phrase | Description |
|---|---|
| `/sync` | Syncs the last 24 hours of data immediately |
| `/sync YYYY-MM-DD` | Syncs a specific date e.g. `/sync 2026-04-15` |
| `/clear` | Clears conversation history for a fresh start |
| `/help` | Shows available commands |
| `"Suggest a gym session"` | Generates a Push/Pull/Legs/Full Body routine and waits for confirmation |
| `"yes"` | Confirms and creates the routine in Hevy |
| `"no"` | Cancels the pending routine |
| Any other message | Chat naturally about your training data |

### Gym routine creation flow
1. Ask "suggest a gym session for today" (or similar)
2. Bot generates a session based on your exercise history, recent sessions, and recovery data
3. Routine is classified as Push, Pull, Legs, or Full Body
4. Bot displays the session in Telegram and asks for confirmation
5. Reply "yes" — routine is created in Hevy with title format "Push - 16 Apr 2026"
6. Open Hevy, find the routine, and start it

Note: Routine suggestions only include exercises already in your Hevy history. As you log more sessions, the exercise library grows automatically.

---

## Environment variables

Set these in Railway:

| Variable | Description |
|---|---|
| `GARMIN_EMAIL` | Garmin Connect login email |
| `GARMIN_PASSWORD` | Garmin Connect password |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase service_role secret key |
| `ANTHROPIC_API_KEY` | Anthropic API key from console.anthropic.com |
| `TELEGRAM_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_USER_ID` | Your Telegram numeric user ID (from @userinfobot) |
| `TRAININGPEAKS_ICAL_URL` | TrainingPeaks calendar iCal URL (use https:// not webcal://) |
| `HEVY_API_KEY` | Hevy API key from hevy.com/settings?developer (PRO required) |
| `SYNC_SECRET` | Any random string used to protect sync endpoints |
| `TZ` | Your local timezone e.g. Australia/Sydney |

---

## Setup

### 1. Supabase — create tables

Run this SQL in the Supabase SQL editor:

```sql
create table daily_wellness (
  id                          uuid primary key default gen_random_uuid(),
  date                        date not null unique,
  hrv_rmssd                   numeric(6,2),
  body_battery_start          integer,
  body_battery_end            integer,
  sleep_score                 numeric(4,1),
  sleep_hours                 numeric(4,2),
  sleep_deep_hours            numeric(4,2),
  sleep_rem_hours             numeric(4,2),
  sleep_light_hours           numeric(4,2),
  sleep_awake_hours           numeric(4,2),
  resting_hr                  numeric(5,1),
  stress_score                numeric(4,1),
  steps                       integer,
  intensity_minutes_moderate  integer,
  intensity_minutes_vigorous  integer,
  spo2                        numeric(4,1),
  respiration_rate            numeric(4,1),
  acute_load                  numeric(6,1),
  training_readiness_score    integer,
  training_readiness_level    text,
  recovery_time_minutes       integer,
  notes                       text,
  synced_at                   timestamp with time zone default now()
);

create table activities (
  id                        uuid primary key default gen_random_uuid(),
  date                      date not null,
  name                      text,
  sport_type                text,
  duration_seconds          integer,
  distance_km               numeric(6,2),
  avg_hr                    integer,
  max_hr                    integer,
  tss                       integer,
  elevation_gain_m          numeric(6,1),
  garmin_activity_id        text unique,
  splits                    jsonb,
  weather                   jsonb,
  compliance_score          integer,
  compliance_notes          text,
  avg_pace_min_km           numeric(5,2),
  avg_cadence               numeric(6,2),
  training_effect_aerobic   numeric(3,1),
  training_effect_anaerobic numeric(3,1),
  exercise_load             numeric(8,2),
  body_battery_impact       numeric(6,2),
  execution_score           numeric(6,2),
  perceived_effort          numeric(6,2),
  stamina_start             numeric(6,2),
  stamina_end               numeric(6,2),
  moving_time_seconds       numeric(8,2),
  calories                  numeric(8,2),
  synced_at                 timestamp with time zone default now()
);

create table training_load (
  id                uuid primary key default gen_random_uuid(),
  date              date not null unique,
  ctl               numeric(5,1),
  atl               numeric(5,1),
  tsb               numeric(5,1),
  ramp_rate         numeric(4,1),
  planned_workout   text,
  workout_completed boolean default false,
  synced_at         timestamp with time zone default now()
);

create table gym_sessions (
  id                uuid primary key default gen_random_uuid(),
  hevy_workout_id   text unique not null,
  date              date not null,
  title             text,
  start_time        timestamp with time zone,
  end_time          timestamp with time zone,
  duration_seconds  integer,
  synced_at         timestamp with time zone default now()
);

create table gym_exercises (
  id                      uuid primary key default gen_random_uuid(),
  gym_session_id          uuid references gym_sessions(id),
  hevy_workout_id         text not null,
  date                    date not null,
  exercise_index          integer,
  exercise_name           text,
  exercise_template_id    text,
  superset_id             text,
  sets                    jsonb,
  total_volume_kg         numeric(8,2),
  max_weight_kg           numeric(6,2),
  total_reps              integer,
  total_duration_seconds  integer,
  synced_at               timestamp with time zone default now(),
  constraint gym_exercises_session_exercise_unique unique (gym_session_id, exercise_index)
);

-- Enable Row Level Security on all tables
alter table daily_wellness enable row level security;
alter table activities enable row level security;
alter table training_load enable row level security;
alter table gym_sessions enable row level security;
alter table gym_exercises enable row level security;
```

### 2. Railway — deploy the backend

1. Fork or clone this repo to your GitHub account
2. Create a new Railway project → Deploy from GitHub repo
3. Add all environment variables listed above including `TZ` for your local timezone
4. Railway will auto-deploy on every push to GitHub
5. Generate a public domain in Railway → Settings → Networking

### 3. Telegram — set up the bot

1. Open Telegram and message @BotFather
2. Send `/newbot` and follow the prompts
3. Copy the token and add it as `TELEGRAM_TOKEN` in Railway
4. Find your user ID by messaging @userinfobot
5. Register the webhook by visiting in your browser:
```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://your-railway-url.up.railway.app/telegram
```

### 4. cron-job.org — schedule daily sync and weekly summary

Create two jobs at [cron-job.org](https://cron-job.org):

**Daily sync** — every day at 7:30am in your timezone:
```
https://your-railway-url.up.railway.app/sync?token=YOUR_SYNC_SECRET
```

**Weekly summary** — every Sunday at 18:00 in your timezone:
```
https://your-railway-url.up.railway.app/weekly-summary?token=YOUR_SYNC_SECRET
```

### 5. Backfill historical data

Once deployed, import the last 90 days of Garmin activities:
```
https://your-railway-url.up.railway.app/backfill?token=YOUR_SYNC_SECRET
```

---

## How sync works

The `/sync` endpoint always pulls the **last 24 hours** of data rather than a fixed yesterday date. This means:

- The 7:30am cron job captures overnight sleep and any previous day activities
- The Telegram `/sync` command captures whatever happened in the last 24 hours regardless of time of day
- All times are calculated in your local timezone via the `TZ` environment variable

---

## Daily flow

**Cardio:**
1. Finish a session on your Garmin Fenix
2. At 7:30am the next morning the cron job pulls all data automatically
3. Or type `/sync` in Telegram anytime for an immediate update
4. Wellness, activities, splits, weather, compliance score land in Supabase
5. Ask your bot about the session

**Gym:**
1. Ask the bot "suggest a gym session for today"
2. Confirm with "yes" — routine is created in Hevy
3. Open Hevy, start the routine, log your sets as you go
4. The next morning the completed workout syncs back into Supabase automatically

---

## Example questions to ask your bot

**Running**
- "How was my recovery this week?"
- "Was my pacing consistent in today's run?"
- "It felt really hard — was the heat a factor?"
- "What's my HRV trend been this week?"
- "How did I go against my planned session?"

**Gym**
- "Suggest a gym session for today"
- "How is my bench press progressing?"
- "How much total volume did I lift this week?"
- "What exercises am I improving on?"

**General**
- "What should I do tomorrow based on my recovery?"
- "Give me a weekly summary"
- "How is my training readiness looking?"
- "Am I overtraining?"

---

## Requirements

```
flask
garminconnect
supabase
anthropic
gunicorn
icalendar
```

---

## Security notes

- All sync endpoints are protected by a secret token
- The Telegram bot only responds to your specific Telegram user ID
- All credentials are stored as Railway environment variables — never in code
- Supabase Row Level Security is enabled on all tables
- Rotate credentials periodically

---

## Notes on Garmin rate limiting

The Garmin Connect unofficial API rate limits login attempts. If you see 429 errors in Railway logs, wait 2-3 hours before trying again. In normal daily use the single 7:30am sync is well within limits. Avoid hitting `/sync` or `/backfill` repeatedly in a short period.
