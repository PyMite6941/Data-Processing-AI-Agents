from fastapi import APIRouter, Depends
from auth import get_user_id
from db import get_db
from collections import defaultdict
from datetime import datetime

router = APIRouter()


def date_key(iso: str) -> str:
    return iso[:10] if iso else ""


@router.get("/")
async def get_chart_data(user_id: str = Depends(get_user_id)):
    db = await get_db()

    workouts_res = await (
        db.table("watch_data")
        .select("*")
        .eq("user_id", user_id)
        .eq("type", "workout")
        .order("timestamp")
        .execute()
    )
    readings_res = await (
        db.table("watch_data")
        .select("timestamp,heart_rate,steps,calories_burned,sleep_hours,spo2,hrv")
        .eq("user_id", user_id)
        .eq("type", "reading")
        .order("timestamp")
        .execute()
    )

    workouts = workouts_res.data or []
    readings = readings_res.data or []

    # calories burned per day (workouts)
    cal_by_day: dict = defaultdict(float)
    for w in workouts:
        if w.get("calories_burned"):
            cal_by_day[date_key(w["timestamp"])] += w["calories_burned"]

    # avg heart rate per day (readings)
    hr_by_day: dict = defaultdict(list)
    for r in readings:
        if r.get("heart_rate"):
            hr_by_day[date_key(r["timestamp"])].append(r["heart_rate"])
    hr_avg_by_day = {d: round(sum(v) / len(v), 1) for d, v in hr_by_day.items()}

    # steps per day
    steps_by_day: dict = defaultdict(int)
    for r in readings:
        if r.get("steps"):
            steps_by_day[date_key(r["timestamp"])] += r["steps"]

    # sleep hours per day
    sleep_by_day: dict = {}
    for r in readings:
        if r.get("sleep_hours"):
            sleep_by_day[date_key(r["timestamp"])] = r["sleep_hours"]

    # workout type breakdown
    type_counts: dict = defaultdict(int)
    for w in workouts:
        if w.get("workout_type"):
            type_counts[w["workout_type"]] += 1

    # recent workouts list (last 10)
    recent = sorted(workouts, key=lambda x: x.get("timestamp", ""), reverse=True)[:10]

    return {
        "calories": {
            "labels": sorted(cal_by_day.keys()),
            "values": [cal_by_day[d] for d in sorted(cal_by_day.keys())],
        },
        "heart_rate": {
            "labels": sorted(hr_avg_by_day.keys()),
            "values": [hr_avg_by_day[d] for d in sorted(hr_avg_by_day.keys())],
        },
        "steps": {
            "labels": sorted(steps_by_day.keys()),
            "values": [steps_by_day[d] for d in sorted(steps_by_day.keys())],
        },
        "sleep": {
            "labels": sorted(sleep_by_day.keys()),
            "values": [sleep_by_day[d] for d in sorted(sleep_by_day.keys())],
        },
        "workout_types": {
            "labels": list(type_counts.keys()),
            "values": list(type_counts.values()),
        },
        "recent_workouts": recent,
    }
