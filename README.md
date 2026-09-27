# Daily Reps — Pushups & Squats

Self-hosted dashboard that sets a daily rep target of `day-of-month × month`,
lets you log pushups/squats, and forwards each log to The Debrief.

## Deploy on TrueNAS Scale

1. Copy this whole folder onto your TrueNAS box (e.g. via SMB/SFTP into a dataset).
2. Edit `docker-compose.yml`:
   - `TZ` — set to your real timezone (e.g. `America/Chicago`). This decides
     when "today" rolls over and which day's target is shown. Defaults to UTC
     if left blank/invalid.
   - `DEBRIEF_URL` — already set to `http://192.168.200.119:5400/api/entries`.
     Change if that ever moves.
3. From that folder:
   ```
   docker compose up -d --build
   ```
4. Open `http://<truenas-ip>:5005`.

Data lives in `./data/daily_reps.db` (SQLite, created automatically, mounted
as a volume so it survives container rebuilds/updates).

## How it works

- **Target**: `month × day-of-month`, computed server-side each request using
  the container's `TZ`. **Easy mode** (toggle on the Overview tab) switches
  the target to just `day-of-month` (no month multiplier) — the setting is
  global and stored in the DB, so it applies retroactively to every stat,
  chart, and badge, past and future, until you toggle it off again.
- **Missed reps**: only counts from Jan 1 *or* from whichever day you first
  logged something, whichever is more recent — so a brand-new install
  doesn't show a huge "missed" number for days before you started using it.
- **Logging**: each exercise has its own log endpoint. Logging again for the
  same day **overwrites** that exercise's value for the day (e.g. log 20,
  then log 40 → stored value is 40, not 60). Pushups and squats are
  independent.
- **Debrief sync**: on every log, the app immediately tries
  `POST http://.../api/entries` with `{"date", "pushups_done", "squats_done"}`
  — only including whichever of those two fields have actually been logged
  locally for that date (so it never overwrites the other exercise's value in
  Debrief with a stray 0). If that request fails, the entry is saved locally,
  a warning banner appears in the UI, and a background worker retries every
  `SYNC_INTERVAL_SECONDS` (default 30s) until it succeeds.
  Beyond that, it's cumulative shortfall (`target - actual`, floored at 0)
  summed over every *completed* day (today is excluded until it's over,
  since it's still "to go" rather than missed).
- **This year**: the Overview stat block shows lifetime pushups/squats
  logged so far this year (Jan 1 through today), regardless of the missed-
  reps start date above.
- **Streak**: consecutive days (working backward from today, or yesterday if
  today isn't done yet) where both pushups and squats met/exceeded that
  day's target.
- **Badges**: computed live from your full log history — lifetime rep
  milestones, single-day records, streak lengths, a "Month Survivor" (a full
  calendar month with zero missed days), halfway-to-annual-target per
  exercise, an early-bird badge (any log before 7am local), a "Balanced
  Build" badge (lifetime pushups/squats within 10% of each other), a
  "Comeback" badge (3+ missed days in a row, then back at it), and "Year
  Finisher" for hitting Dec 31's target of 372.
- **Graphs**: Year view shows cumulative reps vs. the ideal pace curve
  (which is *not* linear — the equation front-loads almost nothing early in
  the year and a lot in Q4, so "% of year elapsed" and "% of pace" will
  diverge, same as your mockup showed). Month view shows a per-day bar chart
  with the day's target as a dashed line. Only months up to the current one
  are selectable.

## Notes / things I assumed

- One thing I simplified from your mockup: the year chart uses straight
  line segments instead of hand-smoothed bezier curves, and badge icons use
  one "earned" checkmark style and one "locked" style rather than a unique
  icon per badge. Functionally identical, just less illustration work.
- If you ever want to backfill or correct a past date (not just today), the
  API supports it (`POST /api/log` accepts an optional `"date"` field) —
  the UI just doesn't expose a date picker yet. Say the word if you want
  that added.
