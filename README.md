# Training Assistant

A personal AI-powered training assistant that automatically syncs data from Garmin, Hevy, and TrainingPeaks into a database, then lets you chat with your training data via Telegram — powered by Claude AI.

---

## What it does

- Automatically pulls your Garmin wellness data (HRV, sleep, Body Battery, resting HR) and activities (runs, rides) including per km splits, weather conditions, training effect, cadence, stamina, execution score, and perceived effort
- Syncs your planned sessions from TrainingPeaks via iCal
- Pulls your gym sessions from Hevy including every exercise, set, reps, and weight
- Scores your workout compliance by comparing planned vs actual sessions
- Sends a HRV fatigue alert via Telegram if your HRV drops 15% below your 7-day average
- Sends an automated weekly training summary every Sunday at 6pm
- Lets you chat naturally with your training data via a private Telegram bot with 5-exchange conversation memory

---

## Architecture

```
Garmin Fenix → Garmin Connect → Railway backend → Supabase
Hevy app → Hevy API → Railway backend → Supabase
TrainingPeaks → iCal feed → Railway backend → Supabase
cron-job.org → /sync (6am daily) → Railway → Supabase
cron-job.org → /weekly-summary (Sunday 6pm) → Telegram
Telegram message → Railway /telegram → Claude AI → Supabase → Telegram reply
```

### Services used

| Service | Purpose | Cost |
|---|---|---|
| Railway | Hosts the Python backend | Free tier |
| Supabase | PostgreSQL database | Free tier |
| Anthropic API | Claude AI for chat responses | ~$1-2/month |
| Telegram | Chat interface | Free |
| Garmin Connect | Activity and wellness data source | Free (unofficial API) |
| Hevy | Gym workout tracking | $3/month PRO required for API |
| TrainingPeaks | Planned session data via iCal | Existing subscription |
| cron-job.org | Daily and weekly scheduled syncs | Free |

---

## Database tables

| Table | Contents |
|---|---|
| `daily_wellness` | HRV, sleep score, sleep hours, Body Battery, resting HR — one row per day |
| `activities` | Runs, rides etc. with splits, weather, training effect, execution score, cadence, stamina, compliance |
| `training_load` | Planned sessions from TrainingPeaks with compliance scoring |
| `gym_sessions` | Hevy workout sessions with duration and title |
| `gym_exercises` | Every exercise with sets, reps, weight, volume, and max weight |

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/sync` | GET | Pulls yesterday's Garmin data + TrainingPeaks + recent Hevy sessions |
| `/sync-date?date=YYYY-MM-DD` | GET | Syncs a specific date — useful for recovering missed days |
| `/weekly-summary` | GET | Generates and sends weekly summary to Telegram |
| `/backfill` | GET | Imports last 90 days of Garmin activities |
| `/telegram` | POST | Telegram bot webhook |
| `/debug-activity?id=XXX` | GET | Returns raw Garmin activity JSON for debugging |
| `/debug-hevy` | GET | Returns last 5 Hevy workouts for debugging |
| `/` | GET | Health check |

All endpoints except `/telegram` require `?token=YOUR_SYNC_SECRET`.

---

## Telegram commands

| Command | Description |
|---|---|
| `/sync` | Syncs today's data immediately |
| `/sync YYYY-MM-DD` | Syncs a specific date e.g. `/sync 2026-04-15` |
| `/clear` | Clears conversation history for a fresh start |
| `/help` | Shows available commands |
| Any message | Chat naturally about your training data |

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
| `HEVY_API_KEY` | Hevy API key (requires Hevy PRO subscription) |
| `SYNC_SECRET` | Any random string used to protect sync endpoints |

---

## Setup

### 1. Supabase — create tables

Run this SQL in the Supabase SQL editor:

```sql
create table daily_wellness (
  id                  uuid primary key default gen_random_uuid(),
  date                date not null unique,
  hrv_rmssd           numeric(6,2),
  body_battery_start  integer,
  body_battery_end    integer,
  sleep_score         numeric(4,1),
  sleep_hours         numeric(4,2),
  resting_hr          numeric(5,1),
  notes               text,
  synced_at           timestamp with time zone default now()
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
3. Add all environment variables listed above
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

**Daily sync** — every day at 6:00 AM in your timezone:
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

## Daily flow

1. You finish a session and save it on your Garmin Fenix
2. Garmin Connect syncs the data
3. At 6am the next morning, the cron job pulls all data automatically
4. Wellness, activities, splits, weather, compliance score all land in Supabase
5. Open Telegram and ask your bot about the session — or type `/sync` for an immediate sync

For gym sessions:
1. Log your session in Hevy with exercises, sets, reps, and weights
2. The daily 6am cron job pulls it from the Hevy API
3. Ask the bot about your lifts, progress, or what to do next session

---

## Example questions to ask your bot

**Running**
- "How was my recovery this week?"
- "Was my pacing consistent in today's run?"
- "It felt really hard — was the heat a factor?"
- "How does my long run compare to last month?"
- "What's my HRV trend been this week?"

**Gym**
- "What gym session should I do today?"
- "How is my bench press progressing?"
- "How much total volume did I lift yesterday?"
- "What exercises am I improving on?"

**General**
- "What should I do tomorrow based on my recovery?"
- "How did I go against my planned session?"
- "Give me a weekly summary"

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

The Garmin Connect unofficial API rate limits login attempts. If you see 429 errors in Railway logs, wait 2-3 hours before trying again. In normal daily use the single 6am sync is well within limits. Avoid hitting `/sync` or `/backfill` repeatedly in a short period.
