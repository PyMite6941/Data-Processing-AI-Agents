from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from auth import get_user_id
from db import get_db
from calories import estimate_calories
import math

router = APIRouter()


class Coordinate(BaseModel):
    lat: float
    lng: float
    timestamp: str
    altitude: Optional[float] = None
    speed: Optional[float] = None


class RoutePayload(BaseModel):
    workout_type: str
    coordinates: list[Coordinate]
    started_at: str
    ended_at: str
    # Sent by the watch so distance/pace/calories are all computed here, not on
    # the device.
    avg_heart_rate: Optional[float] = None
    max_heart_rate: Optional[float] = None
    # True when the client posts ONLY a route (no separate workout) — the watch
    # and the web GPS tracker. Tells us to mirror a workout row into watch_data.
    # Clients that post their own workout (manual entry, Strava/Nike/Garmin
    # imports) leave this false so the workout isn't counted twice.
    record_workout: bool = False
    notes: Optional[str] = None


def haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def calc_distance(coords: list[Coordinate]) -> float:
    total = 0.0
    for i in range(1, len(coords)):
        total += haversine(coords[i-1].lat, coords[i-1].lng, coords[i].lat, coords[i].lng)
    return round(total, 1)


def calc_duration(started_at: str, ended_at: str) -> int:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return 0


def calc_pace(distance_m: float, duration_s: int) -> Optional[str]:
    if distance_m < 1 or duration_s < 1:
        return None
    mins_per_km = (duration_s / 60) / (distance_m / 1000)
    m = int(mins_per_km)
    s = int((mins_per_km - m) * 60)
    return f"{m}:{s:02d} /km"


@router.post("/")
async def save_route(payload: RoutePayload, user_id: str = Depends(get_user_id)):
    """Save a completed GPS route."""
    if len(payload.coordinates) < 2:
        raise HTTPException(status_code=400, detail="Route needs at least 2 coordinates")

    db = await get_db()
    distance = calc_distance(payload.coordinates)
    duration = calc_duration(payload.started_at, payload.ended_at)
    pace = calc_pace(distance, duration)
    duration_min = round(duration / 60.0, 2)

    calories = estimate_calories(duration_min, payload.workout_type)

    record = {
        "user_id": user_id,
        "workout_type": payload.workout_type,
        "coordinates": [c.model_dump() for c in payload.coordinates],
        "distance_meters": distance,
        "duration_seconds": duration,
        "pace": pace,
        "calories_burned": calories,
        "started_at": payload.started_at,
        "ended_at": payload.ended_at,
        "notes": payload.notes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    result = await db.table("routes").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Route insert failed")

    # Route-only clients (the watch, the web GPS tracker) ask us to also record
    # the workout — distance, pace and calories all computed here, server-side,
    # so the device doesn't have to. Clients that post their own workout leave
    # record_workout false to avoid double-counting.
    if payload.record_workout:
        workout_row = {
            "user_id":          user_id,
            "type":             "workout",
            "device":           "gps_route",
            "timestamp":        payload.started_at,
            "workout_type":     payload.workout_type,
            "duration_minutes": duration_min,
            "avg_heart_rate":   payload.avg_heart_rate,
            "max_heart_rate":   payload.max_heart_rate,
            "calories_burned":  calories,
            "distance_meters":  distance,
        }
        try:
            await db.table("watch_data").insert(workout_row).execute()
        except Exception as e:
            # The route saved fine; don't fail the request if the mirror does.
            print(f"[routes] workout mirror insert failed: {e}")

    return result.data[0]


@router.get("/")
async def get_routes(user_id: str = Depends(get_user_id), limit: int = 20):
    """Fetch the user's past routes, newest first."""
    db = await get_db()
    result = await (
        db.table("routes")
        .select("*")
        .eq("user_id", user_id)
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"routes": result.data}


@router.get("/{route_id}")
async def get_route(route_id: str, user_id: str = Depends(get_user_id)):
    """Fetch a single route by ID."""
    db = await get_db()
    result = await (
        db.table("routes")
        .select("*")
        .eq("id", route_id)
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Route not found")
    return result.data
