I want to set up a personal AI training assistant that syncs my Garmin, Hevy, and TrainingPeaks data and lets me chat with an AI coach via Telegram. Please guide me through the setup step by step, one step at a time. Wait for me to confirm when each step is done before moving to the next. Here is the full setup guide:

WHAT WE ARE BUILDING:
A Python backend hosted on Railway that pulls data from Garmin Connect, Hevy, and TrainingPeaks into a Supabase database. A Telegram bot powered by Claude AI lets you chat with your training data. A daily cron job syncs your data automatically at 7:30am. The sync always pulls the last 24 hours of data so it captures sessions regardless of what time you train.

STEP 1 — GITHUB
Fork this repository to your own GitHub account: https://github.com/timbo-max/training-assistant
Go to github.com, sign in or create a free account, then visit the repo URL above and click Fork in the top right. This gives you your own copy of the code.

STEP 2 — SUPABASE DATABASE
Go to supabase.com and create a free account. Create a new project — give it any name and set a strong database password (save this somewhere). Wait for it to finish provisioning (about 2 minutes). Then go to the SQL Editor and run the following SQL to create all the required tables:

create table daily_wellness (id uuid primary key default gen_random_uuid(), date date not null unique, hrv_rmssd numeric(6,2), body_battery_start integer, body_battery_end integer, sleep_score numeric(4,1), sleep_hours numeric(4,2), sleep_deep_hours numeric(4,2), sleep_rem_hours numeric(4,2), sleep_light_hours numeric(4,2), sleep_awake_hours numeric(4,2), resting_hr numeric(5,1), stress_score numeric(4,1), steps integer, intensity_minutes_moderate integer, intensity_minutes_vigorous integer, spo2 numeric(4,1), respiration_rate numeric(4,1), acute_load numeric(6,1), training_readiness_score integer, training_readiness_level text, recovery_time_minutes integer, notes text, synced_at timestamp with time zone default now());

create table activities (id uuid primary key default gen_random_uuid(), date date not null, name text, sport_type text, duration_seconds integer, distance_km numeric(6,2), avg_hr integer, max_hr integer, tss integer, elevation_gain_m numeric(6,1), garmin_activity_id text unique, splits jsonb, weather jsonb, compliance_score integer, compliance_notes text, avg_pace_min_km numeric(5,2), avg_cadence numeric(6,2), training_effect_aerobic numeric(3,1), training_effect_anaerobic numeric(3,1), exercise_load numeric(8,2), body_battery_impact numeric(6,2), execution_score numeric(6,2), perceived_effort numeric(6,2), stamina_start numeric(6,2), stamina_end numeric(6,2), moving_time_seconds numeric(8,2), calories numeric(8,2), synced_at timestamp with time zone default now());

create table training_load (id uuid primary key default gen_random_uuid(), date date not null unique, ctl numeric(5,1), atl numeric(5,1), tsb numeric(5,1), ramp_rate numeric(4,1), planned_workout text, workout_completed boolean default false, synced_at timestamp with time zone default now());

create table gym_sessions (id uuid primary key default gen_random_uuid(), hevy_workout_id text unique not null, date date not null, title text, start_time timestamp with time zone, end_time timestamp with time zone, duration_seconds integer, synced_at timestamp with time zone default now());

create table gym_exercises (id uuid primary key default gen_random_uuid(), gym_session_id uuid references gym_sessions(id), hevy_workout_id text not null, date date not null, exercise_index integer, exercise_name text, exercise_template_id text, superset_id text, sets jsonb, total_volume_kg numeric(8,2), max_weight_kg numeric(6,2), total_reps integer, total_duration_seconds integer, synced_at timestamp with time zone default now(), constraint gym_exercises_session_exercise_unique unique (gym_session_id, exercise_index));

alter table daily_wellness enable row level security;
alter table activities enable row level security;
alter table training_load enable row level security;
alter table gym_sessions enable row level security;
alter table gym_exercises enable row level security;

Once the SQL runs successfully, go to Project Settings → API and copy your Project URL and your service_role secret key. Save both of these.

STEP 3 — ANTHROPIC API KEY
Go to console.anthropic.com, sign up for an account, add a credit card (you will pay per use, roughly $1-2/month), and create an API key. Save it.

STEP 4 — TELEGRAM BOT
Open Telegram on your phone or desktop. Search for @BotFather and start a chat. Send /newbot and follow the prompts to name your bot. BotFather will give you a token — save it. Then search for @userinfobot and send it any message — it will reply with your numeric Telegram user ID. Save that too.

STEP 5 — HEVY API KEY
Log into your Hevy account at hevy.com. Go to Settings → Developer and click Generate API Key. You need a Hevy PRO subscription ($3/month) for this. Save the key.

STEP 6 — TRAININGPEAKS ICAL URL (optional)
Log into TrainingPeaks. Go to your calendar settings and find the iCal/calendar feed URL. It will start with webcal:// — change webcal:// to https:// and save the URL. If you don't use TrainingPeaks, skip this step.

STEP 7 — RAILWAY DEPLOYMENT
Go to railway.app and sign up with your GitHub account. Click New Project → Deploy from GitHub repo → select your forked training-assistant repo. Railway will start building — this takes about 2 minutes. Once deployed, go to Settings → Networking and click Generate Domain. Save the URL (it will look like something.up.railway.app).

Now go to Variables in Railway and add all of the following environment variables one by one:
GARMIN_EMAIL — your Garmin Connect login email
GARMIN_PASSWORD — your Garmin Connect password
SUPABASE_URL — your Supabase project URL from Step 2
SUPABASE_KEY — your Supabase service_role key from Step 2
ANTHROPIC_API_KEY — your Anthropic key from Step 3
TELEGRAM_TOKEN — your bot token from Step 4
TELEGRAM_USER_ID — your numeric Telegram user ID from Step 4
HEVY_API_KEY — your Hevy API key from Step 5
TRAININGPEAKS_ICAL_URL — your iCal URL from Step 6 (if you have it, otherwise skip)
SYNC_SECRET — make up any random string e.g. myname-sync-2026
TZ — your local timezone. Common values: Australia/Sydney (Sydney and Canberra), Australia/Brisbane (Brisbane), Australia/Melbourne (Melbourne), America/New_York (New York), America/Los_Angeles (LA), Europe/London (London)

The TZ variable is important — it makes all sync timestamps use your local time instead of UTC, so the 24 hour sync window is calculated correctly for your location.

After adding all variables Railway will redeploy automatically.

STEP 8 — REGISTER TELEGRAM WEBHOOK
Visit this URL in your browser (replace YOUR_TOKEN and YOUR_RAILWAY_URL with your actual values):
https://api.telegram.org/botYOUR_TOKEN/setWebhook?url=https://YOUR_RAILWAY_URL/telegram
You should see a response saying the webhook was set successfully.

STEP 9 — CRON JOB SETUP
Go to cron-job.org and create a free account. Create two cron jobs:
Job 1 — Daily sync: URL is https://YOUR_RAILWAY_URL/sync?token=YOUR_SYNC_SECRET, schedule is every day at 7:30am in your local timezone.
Job 2 — Weekly summary: URL is https://YOUR_RAILWAY_URL/weekly-summary?token=YOUR_SYNC_SECRET, schedule is every Sunday at 18:00 in your local timezone.

The daily sync pulls the last 24 hours of data so it always captures sessions from the previous day regardless of when you trained.

STEP 10 — BACKFILL HISTORICAL DATA
Visit this URL in your browser to import the last 90 days of your Garmin history:
https://YOUR_RAILWAY_URL/backfill?token=YOUR_SYNC_SECRET
This will take 2-3 minutes. When it returns a success message your historical data is loaded.

STEP 11 — TEST YOUR BOT
Open Telegram and send your bot a message — try asking "How was my training this week?" If it responds with data about your training you are all set up.

You can also type /help to see available commands, and /sync to manually pull the last 24 hours of data at any time.

Please guide me through each of these steps one at a time, starting with Step 1. Ask me to confirm when each step is done before moving on. If I get stuck on any step, help me troubleshoot it.
