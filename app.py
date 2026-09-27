import os
import json
import time
import sqlite3
import calendar
import threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/daily_reps.db")
TZ_NAME = os.environ.get("TZ", "UTC")
DEBRIEF_URL = os.environ.get("DEBRIEF_URL", "http://192.168.200.119:5400/api/entries")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "30"))

try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = ZoneInfo("UTC")

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------- db ----------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS logs(
        date TEXT PRIMARY KEY,
        pushups INTEGER,
        squats INTEGER,
        updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        payload TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""")
    conn.commit()
    conn.close()


# ---------- date / target helpers ----------

def today_local() -> date:
    return datetime.now(TZ).date()


def day_target(d: date) -> int:
    return d.day * d.month


def days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def iter_year_days(year: int):
    d = date(year, 1, 1)
    end = date(year, 12, 31)
    while d <= end:
        yield d
        d += timedelta(days=1)


def get_row(conn, d: date):
    return conn.execute("SELECT * FROM logs WHERE date=?", (d.isoformat(),)).fetchone()


def get_logs_map(conn, year: int):
    rows = conn.execute("SELECT * FROM logs WHERE date LIKE ?", (f"{year}-%",)).fetchall()
    return {r["date"]: r for r in rows}


def pv(r):
    return r["pushups"] if r and r["pushups"] is not None else 0


def sv(r):
    return r["squats"] if r and r["squats"] is not None else 0


# ---------- writes ----------

def upsert_log(conn, d: date, exercise: str, value: int):
    row = get_row(conn, d)
    pushups = row["pushups"] if row else None
    squats = row["squats"] if row else None
    if exercise == "pushups":
        pushups = value
    else:
        squats = value
    now = datetime.now(TZ).isoformat()
    conn.execute(
        """INSERT INTO logs(date,pushups,squats,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET pushups=excluded.pushups,
               squats=excluded.squats, updated_at=excluded.updated_at""",
        (d.isoformat(), pushups, squats, now),
    )
    conn.commit()
    return get_row(conn, d)


# ---------- Debrief sync ----------

def build_debrief_payload(row):
    # Only include fields that have actually been logged locally, so we never
    # blast a 0 over a value Debrief already has for the other exercise.
    payload = {"date": row["date"]}
    if row["pushups"] is not None:
        payload["pushups_done"] = row["pushups"]
    if row["squats"] is not None:
        payload["squats_done"] = row["squats"]
    return payload


def try_send_debrief(payload):
    try:
        resp = requests.post(DEBRIEF_URL, json=payload, timeout=4)
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)


def enqueue_outbox(conn, d: date, payload: dict):
    conn.execute(
        "INSERT INTO outbox(date,payload,created_at,status) VALUES(?,?,?,?)",
        (d.isoformat(), json.dumps(payload), datetime.now(TZ).isoformat(), "pending"),
    )
    conn.commit()


def sync_worker():
    while True:
        try:
            conn = get_db()
            pending = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY id"
            ).fetchall()
            for item in pending:
                ok, err = try_send_debrief(json.loads(item["payload"]))
                if ok:
                    conn.execute("UPDATE outbox SET status='sent' WHERE id=?", (item["id"],))
                else:
                    conn.execute(
                        "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                        (err, item["id"]),
                    )
                conn.commit()
            conn.close()
        except Exception as e:
            print("sync_worker error:", e, flush=True)
        time.sleep(SYNC_INTERVAL)


# ---------- routes ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/log", methods=["POST"])
def api_log():
    body = request.get_json(force=True, silent=True) or {}
    exercise = body.get("exercise")
    value = body.get("value")
    date_str = body.get("date")

    if exercise not in ("pushups", "squats"):
        return jsonify({"error": "exercise must be 'pushups' or 'squats'"}), 400
    try:
        value = int(value)
        if value < 0:
            raise ValueError()
    except Exception:
        return jsonify({"error": "value must be a non-negative integer"}), 400

    try:
        d = today_local() if not date_str else date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    conn = get_db()
    row = upsert_log(conn, d, exercise, value)
    payload = build_debrief_payload(row)
    ok, err = try_send_debrief(payload)
    if not ok:
        enqueue_outbox(conn, d, payload)
    conn.close()

    return jsonify({
        "ok": True,
        "synced": ok,
        "sync_error": err,
        "date": d.isoformat(),
        "pushups": row["pushups"],
        "squats": row["squats"],
    })


@app.route("/api/state")
def api_state():
    conn = get_db()
    today = today_local()
    year = today.year
    target_today = day_target(today)
    row_today = get_row(conn, today)

    logs_map = get_logs_map(conn, year)

    total_year_target = sum(day_target(d) for d in iter_year_days(year))
    cum_target_today = 0
    cum_pushups = 0
    cum_squats = 0
    missed_pushups = 0
    missed_squats = 0

    for d in iter_year_days(year):
        if d > today:
            break
        cum_target_today += day_target(d)
        r = logs_map.get(d.isoformat())
        p, s = pv(r), sv(r)
        cum_pushups += p
        cum_squats += s
        if d < today:
            t = day_target(d)
            missed_pushups += max(t - p, 0)
            missed_squats += max(t - s, 0)

    elapsed_pct = today.timetuple().tm_yday / days_in_year(year) * 100
    pace_pct = cum_target_today / total_year_target * 100
    actual_pct = (cum_pushups + cum_squats) / (total_year_target * 2) * 100
    gap_pts = elapsed_pct - pace_pct

    def fully_met(d):
        r = logs_map.get(d.isoformat())
        if not r:
            return False
        t = day_target(d)
        return pv(r) >= t and sv(r) >= t

    streak = 0
    cur = today if fully_met(today) else today - timedelta(days=1)
    while fully_met(cur):
        streak += 1
        cur -= timedelta(days=1)

    pending_sync = conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='pending'"
    ).fetchone()["c"]
    last_err_row = conn.execute(
        "SELECT last_error FROM outbox WHERE status='pending' AND last_error IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    return jsonify({
        "date": today.isoformat(),
        "day": today.day,
        "month": today.month,
        "day_of_year": today.timetuple().tm_yday,
        "days_in_year": days_in_year(year),
        "target_today": target_today,
        "pushups_today": pv(row_today),
        "squats_today": sv(row_today),
        "pushups_logged": bool(row_today and row_today["pushups"] is not None),
        "squats_logged": bool(row_today and row_today["squats"] is not None),
        "missed_pushups": missed_pushups,
        "missed_squats": missed_squats,
        "streak": streak,
        "elapsed_pct": round(elapsed_pct, 1),
        "pace_pct": round(pace_pct, 1),
        "actual_pct": round(actual_pct, 1),
        "gap_pts": round(gap_pts, 1),
        "pending_sync": pending_sync,
        "last_sync_error": last_err_row["last_error"] if last_err_row else None,
    })


@app.route("/api/chart/year")
def api_chart_year():
    conn = get_db()
    today = today_local()
    year = today.year
    logs_map = get_logs_map(conn, year)
    conn.close()

    out = []
    cum_target = cum_p = cum_s = 0
    for d in iter_year_days(year):
        cum_target += day_target(d)
        entry = {"date": d.isoformat(), "target_cum": cum_target}
        if d <= today:
            r = logs_map.get(d.isoformat())
            cum_p += pv(r)
            cum_s += sv(r)
            entry["pushups_cum"] = cum_p
            entry["squats_cum"] = cum_s
        out.append(entry)
    return jsonify(out)


@app.route("/api/chart/month/<int:m>")
def api_chart_month(m):
    if m < 1 or m > 12:
        return jsonify({"error": "bad month"}), 400
    conn = get_db()
    today = today_local()
    year = today.year
    if date(year, m, 1) > today:
        conn.close()
        return jsonify([])
    logs_map = get_logs_map(conn, year)
    conn.close()

    days = calendar.monthrange(year, m)[1]
    out = []
    for day in range(1, days + 1):
        d = date(year, m, day)
        if d > today:
            break
        r = logs_map.get(d.isoformat())
        out.append([day, day_target(d), pv(r), sv(r)])
    return jsonify(out)


@app.route("/api/badges")
def api_badges():
    conn = get_db()
    today = today_local()
    rows = conn.execute("SELECT * FROM logs ORDER BY date").fetchall()
    conn.close()

    logs = {r["date"]: r for r in rows}
    lifetime_pushups = sum(pv(r) for r in rows)
    lifetime_squats = sum(sv(r) for r in rows)
    lifetime_total = lifetime_pushups + lifetime_squats
    max_single_day = max([0] + [max(pv(r), sv(r)) for r in rows])

    def fully_met(d):
        r = logs.get(d.isoformat())
        if not r:
            return False
        t = day_target(d)
        return pv(r) >= t and sv(r) >= t

    def streak_ending(d):
        n = 0
        cur = d
        while fully_met(cur):
            n += 1
            cur -= timedelta(days=1)
        return n

    current_streak = streak_ending(today) if fully_met(today) else streak_ending(today - timedelta(days=1))

    best_streak = 0
    if rows:
        d = date.fromisoformat(rows[0]["date"])
        while d <= today:
            if fully_met(d):
                best_streak = max(best_streak, streak_ending(d))
            d += timedelta(days=1)

    month_survivor = False
    for m in range(1, today.month):
        days = calendar.monthrange(today.year, m)[1]
        if all(fully_met(date(today.year, m, day)) for day in range(1, days + 1)):
            month_survivor = True
            break

    total_year_target = sum(day_target(d) for d in iter_year_days(today.year))
    half = total_year_target / 2

    early_bird = False
    for r in rows:
        try:
            if datetime.fromisoformat(r["updated_at"]).hour < 7:
                early_bird = True
                break
        except Exception:
            pass

    balanced = (
        lifetime_total >= 500
        and lifetime_pushups > 0
        and lifetime_squats > 0
        and abs(lifetime_pushups - lifetime_squats) / max(lifetime_pushups, lifetime_squats) <= 0.10
    )

    dec31 = logs.get(date(today.year, 12, 31).isoformat())
    year_finisher = dec31 is not None and pv(dec31) >= 372 and sv(dec31) >= 372

    comeback = False
    if len(rows) >= 2:
        first_d = date.fromisoformat(rows[0]["date"])
        gap = 0
        d = first_d
        while d <= today:
            r = logs.get(d.isoformat())
            missed_day = (r is None) or (pv(r) == 0 and sv(r) == 0)
            if missed_day:
                gap += 1
            else:
                if gap >= 3:
                    comeback = True
                    break
                gap = 0
            d += timedelta(days=1)

    badges = [
        {"id": "first_rep", "name": "First Rep", "desc": "Logged your very first workout.",
         "earned": len(rows) > 0},
        {"id": "century_club", "name": "Century Club", "desc": "100 lifetime reps across both exercises.",
         "earned": lifetime_total >= 100},
        {"id": "four_figures", "name": "Four Figures", "desc": "1,000 lifetime reps.",
         "earned": lifetime_total >= 1000},
        {"id": "five_figures", "name": "Five Figures", "desc": "10,000 lifetime reps.",
         "earned": lifetime_total >= 10000},
        {"id": "double_century", "name": "Double Century",
         "desc": "200+ reps of one exercise in a single day.", "earned": max_single_day >= 200},
        {"id": "triple_century", "name": "Triple Century",
         "desc": "300+ reps of one exercise in a single day.", "earned": max_single_day >= 300},
        {"id": "week_streak", "name": "Week Streak",
         "desc": "Both exercises, every day, for 7 days straight.", "earned": best_streak >= 7},
        {"id": "month_streak", "name": "Month Streak",
         "desc": "Both exercises, every day, for 30 days straight.", "earned": best_streak >= 30},
        {"id": "century_streak", "name": "Century Streak",
         "desc": "Both exercises, every day, for 100 days straight.", "earned": best_streak >= 100},
        {"id": "month_survivor", "name": "Month Survivor",
         "desc": "Finish a full calendar month with zero missed days.", "earned": month_survivor},
        {"id": "halfway_pushups", "name": "Halfway Home \u2014 Pushups",
         "desc": "Reached 50% of the year's pushup target.", "earned": lifetime_pushups >= half},
        {"id": "halfway_squats", "name": "Halfway Home \u2014 Squats",
         "desc": "Reached 50% of the year's squat target.", "earned": lifetime_squats >= half},
        {"id": "early_bird", "name": "Early Bird", "desc": "Logged a workout before 7am.",
         "earned": early_bird},
        {"id": "balanced_build", "name": "Balanced Build",
         "desc": "Lifetime pushups and squats within 10% of each other (500+ total).", "earned": balanced},
        {"id": "comeback", "name": "Comeback",
         "desc": "Missed 3+ days in a row, then got back on the horse.", "earned": comeback},
        {"id": "year_finisher", "name": "Year Finisher",
         "desc": "Hit the hardest day, Dec 31 \u2014 12 \u00d7 31 = 372.", "earned": year_finisher},
    ]

    return jsonify({"badges": badges, "current_streak": current_streak, "best_streak": best_streak})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=sync_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5005)
