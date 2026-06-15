from fastapi import APIRouter, Depends
from auth import get_user_id
from db import get_db
from datetime import datetime, timezone, timedelta

router = APIRouter()


@router.get("/me")
async def get_me(user_id: str = Depends(get_user_id)):
    """Return basic stats for the authenticated user."""
    db = await get_db()

    readings = await db.table("watch_data").select("id", count="exact").eq("user_id", user_id).eq("type", "reading").execute()
    workouts = await db.table("watch_data").select("id", count="exact").eq("user_id", user_id).eq("type", "workout").execute()
    analyses = await db.table("analyses").select("id", count="exact").eq("user_id", user_id).execute()

    return {
        "user_id": user_id,
        "readings": readings.count,
        "workouts": workouts.count,
        "analyses": analyses.count,
    }


@router.get("/history")
async def get_history(user_id: str = Depends(get_user_id), limit: int = 20):
    """Return the user's past AI analyses, newest first — consumed by the phone."""
    db = await get_db()
    result = await (
        db.table("analyses")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"analyses": result.data}


@router.get("/summary")
async def get_summary(user_id: str = Depends(get_user_id)):
    """Weekly comparison stats, streak, and personal records."""
    db = await get_db()
    now = datetime.now(timezone.utc)
    seven_ago    = now - timedelta(days=7)
    fourteen_ago = now - timedelta(days=14)
    sixty_ago    = now - timedelta(days=60)

    # ── Last 14 days for week-over-week comparison ─────────────────────
    recent = await (
        db.table("watch_data").select("*")
        .eq("user_id", user_id)
        .gte("timestamp", fourteen_ago.isoformat())
        .execute()
    )
    rows = recent.data or []
    seven_str = seven_ago.isoformat()
    this_week = [r for r in rows if (r.get("timestamp") or "") >= seven_str]
    last_week = [r for r in rows if (r.get("timestamp") or "") <  seven_str]

    def week_stats(week_rows):
        workouts = [r for r in week_rows if r.get("type") == "workout"]
        readings = [r for r in week_rows if r.get("type") == "reading"]
        hrs   = [r.get("avg_heart_rate") or r.get("heart_rate") for r in week_rows
                 if r.get("avg_heart_rate") or r.get("heart_rate")]
        steps = [r.get("steps") or 0 for r in readings]
        sleep = [r.get("sleep_hours") for r in readings if r.get("sleep_hours")]
        return {
            "workouts":    len(workouts),
            "distance_km": round(sum(r.get("distance_meters") or 0 for r in workouts) / 1000, 1),
            "calories":    round(sum(r.get("calories_burned") or 0 for r in workouts)),
            "avg_hr":      round(sum(hrs) / len(hrs)) if hrs else 0,
            "total_steps": sum(steps),
            "avg_sleep":   round(sum(sleep) / len(sleep), 1) if sleep else 0,
        }

    # ── Streak (consecutive days with a workout) ───────────────────────
    streak_res = await (
        db.table("watch_data").select("timestamp")
        .eq("user_id", user_id).eq("type", "workout")
        .gte("timestamp", sixty_ago.isoformat())
        .execute()
    )
    active_days = {(r.get("timestamp") or "")[:10] for r in (streak_res.data or []) if r.get("timestamp")}
    streak, check = 0, now.date()
    while str(check) in active_days:
        streak += 1
        check -= timedelta(days=1)

    # ── Personal records ───────────────────────────────────────────────
    w_all = await (db.table("watch_data").select("distance_meters,calories_burned,heart_rate,steps,timestamp")
                   .eq("user_id", user_id).execute())
    r_all = await (db.table("routes").select("distance_meters,duration_seconds")
                   .eq("user_id", user_id).execute())

    w_rows = w_all.data or []
    workout_rows  = [r for r in w_rows if r.get("distance_meters") or r.get("calories_burned")]
    reading_rows  = [r for r in w_rows if r.get("heart_rate") or r.get("steps")]

    max_dist_m = max((r.get("distance_meters") or 0 for r in workout_rows), default=0)
    max_cal    = max((r.get("calories_burned") or 0 for r in workout_rows), default=0)
    max_hr     = max((r.get("heart_rate") or 0 for r in reading_rows), default=0)

    steps_by_day: dict = {}
    for r in reading_rows:
        day = (r.get("timestamp") or "")[:10]
        if day:
            steps_by_day[day] = steps_by_day.get(day, 0) + (r.get("steps") or 0)
    max_steps_day = max(steps_by_day.values(), default=0)

    best_pace_str, best_pace_val = None, float("inf")
    for r in (r_all.data or []):
        dist, dur = r.get("distance_meters") or 0, r.get("duration_seconds") or 0
        if dist > 500 and dur > 0:
            pace = (dur / 60) / (dist / 1000)
            if pace < best_pace_val:
                best_pace_val = pace
                m, s = int(pace), int((pace % 1) * 60)
                best_pace_str = f"{m}:{s:02d}"

    return {
        "streak":    streak,
        "this_week": week_stats(this_week),
        "last_week": week_stats(last_week),
        "records": {
            "longest_km":   round(max_dist_m / 1000, 1) if max_dist_m else None,
            "max_calories": round(max_cal)  if max_cal  else None,
            "max_hr":       round(max_hr)   if max_hr   else None,
            "max_steps":    max_steps_day   if max_steps_day else None,
            "best_pace":    best_pace_str,
        },
    }


@router.get("/data")
async def get_watch_data(user_id: str = Depends(get_user_id), limit: int = 100):
    """Return raw watch data for the user, newest first."""
    db = await get_db()
    result = await (
        db.table("watch_data")
        .select("*")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return {"data": result.data}
