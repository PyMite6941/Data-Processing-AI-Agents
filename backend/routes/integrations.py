import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from auth import get_user_id
from db import get_db
from datetime import datetime, timezone, timedelta

router = APIRouter()

STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
BACKEND_URL          = os.getenv("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL         = os.getenv("FRONTEND_URL", "http://localhost:5173")

_OAUTH_SECRET = os.getenv("OAUTH_STATE_SECRET", os.urandom(32).hex())

STRAVA_AUTH_URL    = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL   = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES  = "https://www.strava.com/api/v3/athlete/activities"
STRAVA_STREAMS_URL = "https://www.strava.com/api/v3/activities/{id}/streams"

NRC_TYPE_MAP = {
    "nike.run.gps":       "running",
    "nike.run.manual":    "running",
    "nike.run.treadmill": "running",
    "nike.sport.walk":    "walking",
    "nike.sport.hike":    "hiking",
    "nike.sport.cycle":   "cycling",
    "nike.sport.swim":    "swimming",
    "run":   "running",
    "walk":  "walking",
    "hike":  "hiking",
    "cycle": "cycling",
    "swim":  "swimming",
}

STRAVA_TYPE_MAP = {
    "Run": "running", "VirtualRun": "running",
    "Ride": "cycling", "VirtualRide": "cycling", "EBikeRide": "cycling",
    "Walk": "walking",
    "Hike": "hiking",
    "WeightTraining": "weightlifting",
    "Swim": "swimming",
    "Workout": "other", "Crossfit": "other", "Elliptical": "other",
    "RockClimbing": "hiking", "Yoga": "other", "Pilates": "other",
}


@router.get("/status")
async def integration_status(user_id: str = Depends(get_user_id)):
    db = await get_db()
    result = await (
        db.table("user_integrations")
        .select("provider")
        .eq("user_id", user_id)
        .execute()
    )
    return {row["provider"]: True for row in (result.data or [])}


def _make_state(user_id: str) -> str:
    sig = hmac.new(_OAUTH_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def _verify_state(state: str) -> str | None:
    """Return user_id if state is valid, else None."""
    try:
        user_id, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_OAUTH_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return user_id
    return None


@router.get("/strava/connect")
async def strava_connect(user_id: str = Depends(get_user_id)):
    callback = f"{BACKEND_URL}/integrations/strava/callback"
    url = (
        f"{STRAVA_AUTH_URL}"
        f"?client_id={STRAVA_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&response_type=code"
        f"&scope=activity:read_all"
        f"&state={_make_state(user_id)}"
        f"&approval_prompt=auto"
    )
    return {"url": url}


@router.get("/strava/callback")
async def strava_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"{FRONTEND_URL}/log?error={error or 'access_denied'}")

    user_id = _verify_state(state)
    if not user_id:
        return RedirectResponse(f"{FRONTEND_URL}/log?error=invalid_state")
    async with httpx.AsyncClient() as client:
        resp = await client.post(STRAVA_TOKEN_URL, data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
        })
        if resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/log?error=token_exchange_failed")
        data = resp.json()

    db = await get_db()
    row = {
        "user_id":       user_id,
        "provider":      "strava",
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at":    datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).isoformat(),
        "athlete_id":    str(data.get("athlete", {}).get("id", "")),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }
    await db.table("user_integrations").upsert(row, on_conflict="user_id,provider").execute()
    return RedirectResponse(f"{FRONTEND_URL}/log?connected=strava")


@router.post("/strava/sync")
async def strava_sync(user_id: str = Depends(get_user_id)):
    """Fetch latest Strava activities. Saves workouts to watch_data and GPS routes to routes table."""
    db = await get_db()

    integration = await (
        db.table("user_integrations")
        .select("*")
        .eq("user_id", user_id)
        .eq("provider", "strava")
        .single()
        .execute()
    )
    if not integration.data:
        return {"synced": 0, "routes_saved": 0, "error": "Strava not connected"}

    token = await _refresh_strava_token_if_needed(db, integration.data)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            STRAVA_ACTIVITIES,
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": 30},
        )
        if resp.status_code != 200:
            return {"synced": 0, "routes_saved": 0, "error": "Failed to fetch Strava activities"}
        activities = resp.json()

        workout_rows = []
        route_rows   = []

        for a in activities:
            start_date = a.get("start_date", "")
            if not start_date:
                continue  # skip activities without a date — we can't place them in time
            workout_type = STRAVA_TYPE_MAP.get(a.get("type", ""), "other")

            # ── Workout record ──────────────────────────────────────────
            workout_rows.append({
                "user_id":            user_id,
                "type":               "workout",
                "device":             "strava",
                "timestamp":          start_date,
                "workout_type":       workout_type,
                "duration_minutes":   round(a.get("moving_time", 0) / 60, 1),
                "distance_meters":    a.get("distance"),
                "calories_burned":    a.get("calories"),
                "avg_heart_rate":     a.get("average_heartrate"),
                "max_heart_rate":     a.get("max_heartrate"),
                "ending_heart_rate":  None,   # not in activity summary; streams would be needed
                "notes":              a.get("name"),
            })

            # ── GPS route — only for activities that have a map ─────────
            if a.get("map", {}).get("summary_polyline"):
                streams_resp = await client.get(
                    STRAVA_STREAMS_URL.format(id=a["id"]),
                    headers={"Authorization": f"Bearer {token}"},
                    params={"keys": "latlng,time,heartrate", "key_by_type": "true"},
                )
                if streams_resp.status_code == 200:
                    streams   = streams_resp.json()
                    latlng    = streams.get("latlng",    {}).get("data", [])
                    times     = streams.get("time",      {}).get("data", [])
                    hr_stream = streams.get("heartrate", {}).get("data", [])

                    if len(latlng) >= 2:
                        try:
                            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                        except Exception:
                            continue  # can't build a route without a valid start time

                        elapsed  = a.get("elapsed_time", 0)
                        ended_dt = start_dt + timedelta(seconds=elapsed)

                        coords = []
                        for i, pt in enumerate(latlng):
                            offset_s = times[i] if i < len(times) else 0
                            ts = (start_dt + timedelta(seconds=offset_s)).isoformat()
                            c = {"lat": pt[0], "lng": pt[1], "timestamp": ts}
                            if i < len(hr_stream) and hr_stream[i]:
                                c["heart_rate"] = hr_stream[i]
                            coords.append(c)

                        # ending HR = last HR value in stream
                        ending_hr = None
                        for val in reversed(hr_stream):
                            if val:
                                ending_hr = val
                                break
                        # Back-fill ending_heart_rate onto the workout row
                        workout_rows[-1]["ending_heart_rate"] = ending_hr

                        route_rows.append({
                            "user_id":          user_id,
                            "workout_type":     workout_type,
                            "coordinates":      coords,
                            "distance_meters":  a.get("distance"),
                            "duration_seconds": a.get("moving_time"),
                            "started_at":       start_date,
                            "ended_at":         ended_dt.isoformat(),
                            "notes":            a.get("name"),
                        })

    if workout_rows:
        await db.table("watch_data").insert(workout_rows).execute()
    if route_rows:
        await db.table("routes").insert(route_rows).execute()

    return {
        "synced":       len(workout_rows),
        "routes_saved": len(route_rows),
    }


@router.post("/nike/import")
async def nike_import(
    file: UploadFile = File(...),
    user_id: str = Depends(get_user_id),
):
    """Parse a Nike Run Club data-export ZIP or JSON and save activities to watch_data."""
    content = await file.read()
    activities: list[dict] = []

    filename = (file.filename or "").lower()
    if filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".json"):
                        continue
                    with zf.open(name) as f:
                        try:
                            data = json.load(f)
                            if isinstance(data, list):
                                activities.extend(data)
                            elif isinstance(data, dict) and ("startEpochMs" in data or "type" in data):
                                activities.append(data)
                        except Exception:
                            pass
        except zipfile.BadZipFile:
            return {"error": "Invalid ZIP file", "imported": 0, "routes_saved": 0}
    else:
        try:
            data = json.loads(content)
            activities = data if isinstance(data, list) else [data]
        except Exception:
            return {"error": "Invalid JSON file", "imported": 0, "routes_saved": 0}

    workout_rows: list[dict] = []
    route_rows:   list[dict] = []

    for a in activities:
        # ── Timestamp ─────────────────────────────────────────────────────
        start_ms = a.get("startEpochMs") or a.get("start_epoch_ms")
        if start_ms:
            start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        else:
            continue  # skip entries with no timestamp — we can't place them in time

        # ── Duration ──────────────────────────────────────────────────────
        active_ms   = a.get("activeTime") or a.get("active_duration_ms") or 0
        duration_min = round(active_ms / 60000, 1) if active_ms else None

        # ── Activity type ─────────────────────────────────────────────────
        workout_type = NRC_TYPE_MAP.get(a.get("type", "").lower(), "other")

        # ── Tags (NRC stores extra fields here) ───────────────────────────
        tags = a.get("tags", {})

        def _tag(key: str):
            return tags.get(key) or tags.get(f"com.nike.{key}")

        # ── Distance ──────────────────────────────────────────────────────
        distance_m = None
        dist_raw = _tag("distance") or a.get("distance")
        if dist_raw is not None:
            try:
                if isinstance(dist_raw, dict):
                    val  = float(dist_raw.get("value", 0))
                    unit = dist_raw.get("unit", "KM").upper()
                    distance_m = round(val * 1000 if unit in ("KM", "KILOMETERS") else val * 1609.34, 1)
                else:
                    distance_m = round(float(dist_raw) * 1000, 1)  # NRC tags are in km
            except (ValueError, TypeError):
                pass

        # ── Calories ──────────────────────────────────────────────────────
        calories = None
        cal_raw = _tag("calories") or a.get("calories")
        if cal_raw is not None:
            try:
                calories = float(cal_raw.get("value", 0) if isinstance(cal_raw, dict) else cal_raw)
            except (ValueError, TypeError):
                pass

        # ── Heart rate + GPS from metrics ─────────────────────────────────
        avg_hr = max_hr = ending_hr = None
        lat_entries: list[dict] = []
        lng_entries: list[dict] = []

        for metric in a.get("metrics", []):
            mtype  = metric.get("type", "").upper()
            values = metric.get("values", [])
            if not values:
                continue

            if mtype == "HEART_RATE":
                hr_vals = [v["value"] for v in values if v.get("value") is not None]
                if hr_vals:
                    avg_hr    = round(sum(hr_vals) / len(hr_vals))
                    max_hr    = int(max(hr_vals))
                    ending_hr = int(hr_vals[-1])
            elif mtype == "LATITUDE":
                lat_entries = values
            elif mtype == "LONGITUDE":
                lng_entries = values

        # Also check summaries block for distance/calories if still missing
        for s in a.get("summaries", []):
            stype = s.get("metric", "").upper()
            val   = s.get("value")
            if val is None:
                continue
            if stype == "DISTANCE" and distance_m is None:
                distance_m = round(float(val) * 1000, 1)
            elif stype == "CALORIES" and calories is None:
                calories = float(val)

        # ── Workout row ───────────────────────────────────────────────────
        workout_rows.append({
            "user_id":           user_id,
            "type":              "workout",
            "device":            "nike_run_club",
            "timestamp":         start_dt.isoformat(),
            "workout_type":      workout_type,
            "duration_minutes":  duration_min,
            "distance_meters":   distance_m,
            "calories_burned":   calories,
            "avg_heart_rate":    avg_hr,
            "max_heart_rate":    max_hr,
            "ending_heart_rate": ending_hr,
            "notes":             _tag("name") or a.get("name") or a.get("title"),
        })

        # ── GPS route ─────────────────────────────────────────────────────
        if len(lat_entries) >= 2 and len(lng_entries) >= 2:
            coords = []
            for i, lat_entry in enumerate(lat_entries):
                if i >= len(lng_entries):
                    break
                ts_ms = lat_entry.get("startEpochMs") or start_ms
                coords.append({
                    "lat":       lat_entry.get("value"),
                    "lng":       lng_entries[i].get("value"),
                    "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
                })
            if len(coords) >= 2:
                ended_dt = start_dt + timedelta(milliseconds=active_ms or 0)
                route_rows.append({
                    "user_id":          user_id,
                    "workout_type":     workout_type,
                    "coordinates":      coords,
                    "distance_meters":  distance_m,
                    "duration_seconds": active_ms // 1000 if active_ms else None,
                    "started_at":       start_dt.isoformat(),
                    "ended_at":         ended_dt.isoformat(),
                    "notes":            _tag("name") or a.get("name"),
                })

    db = await get_db()
    if workout_rows:
        await db.table("watch_data").insert(workout_rows).execute()
    if route_rows:
        await db.table("routes").insert(route_rows).execute()

    return {"imported": len(workout_rows), "routes_saved": len(route_rows)}


async def _refresh_strava_token_if_needed(db, integration: dict) -> str:
    expires_at = datetime.fromisoformat(integration["expires_at"])
    if datetime.now(timezone.utc) < expires_at:
        return integration["access_token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(STRAVA_TOKEN_URL, data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": integration["refresh_token"],
            "grant_type":    "refresh_token",
        })

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to refresh Strava token")

    data = resp.json()
    if "access_token" not in data:
        raise HTTPException(status_code=502, detail="Strava token response missing access_token")

    await (
        db.table("user_integrations")
        .update({
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", integration["refresh_token"]),
            "expires_at":    datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).isoformat(),
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", integration["id"])
        .execute()
    )
    return data["access_token"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _classify_activity(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ("run", "jog")):           return "running"
    if any(w in n for w in ("cycl", "bike", "rid")):  return "cycling"
    if any(w in n for w in ("walk",)):                 return "walking"
    if any(w in n for w in ("hike", "trail")):         return "hiking"
    if any(w in n for w in ("swim",)):                 return "swimming"
    if any(w in n for w in ("weight", "strength", "lift", "gym")): return "weightlifting"
    return "other"


def _strip_ns(root: ET.Element) -> ET.Element:
    """Remove XML namespace prefixes so we can find tags by local name."""
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _parse_apple_date(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse Apple Health date: {s!r}")


# ── Google Health API (Fitbit wearable data via Google OAuth) ─────────────────
# Fitbit Web API was deprecated; new apps register at Google Cloud Console.
# Docs: https://developers.google.com/health/api

GOOGLE_HEALTH_CLIENT_ID     = os.getenv("GOOGLE_HEALTH_CLIENT_ID", "")
GOOGLE_HEALTH_CLIENT_SECRET = os.getenv("GOOGLE_HEALTH_CLIENT_SECRET", "")
GOOGLE_AUTH_URL             = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL            = "https://oauth2.googleapis.com/token"
GOOGLE_HEALTH_BASE          = "https://health.googleapis.com/v4"

GOOGLE_HEALTH_SCOPES = " ".join([
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
])

GOOGLE_EXERCISE_TYPE_MAP = {
    "RUNNING":           "running",
    "WALKING":           "walking",
    "BIKING":            "cycling",
    "SWIMMING":          "swimming",
    "HIKING":            "hiking",
    "YOGA":              "other",
    "PILATES":           "other",
    "WORKOUT":           "other",
    "HIIT":              "other",
    "WEIGHTLIFTING":     "weightlifting",
    "STRENGTH_TRAINING": "weightlifting",
    "OTHER":             "other",
}


def _parse_google_time(t) -> str | None:
    """Accept RFC 3339 string or proto Timestamp dict {seconds, nanos}."""
    if isinstance(t, str):
        return t.replace("Z", "+00:00")
    if isinstance(t, dict):
        secs = t.get("seconds") or t.get("epochSeconds")
        if secs:
            return datetime.fromtimestamp(int(secs), tz=timezone.utc).isoformat()
    return None


@router.get("/fitbit/connect")
async def fitbit_connect(user_id: str = Depends(get_user_id)):
    callback = f"{BACKEND_URL}/integrations/fitbit/callback"
    url = (
        f"{GOOGLE_AUTH_URL}"
        f"?client_id={GOOGLE_HEALTH_CLIENT_ID}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(GOOGLE_HEALTH_SCOPES)}"
        f"&redirect_uri={urllib.parse.quote(callback, safe='')}"
        f"&state={_make_state(user_id)}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return {"url": url}


@router.get("/fitbit/callback")
async def fitbit_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"{FRONTEND_URL}/log?error={error or 'access_denied'}")
    user_id = _verify_state(state)
    if not user_id:
        return RedirectResponse(f"{FRONTEND_URL}/log?error=invalid_state")

    callback = f"{BACKEND_URL}/integrations/fitbit/callback"
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  callback,
            "client_id":     GOOGLE_HEALTH_CLIENT_ID,
            "client_secret": GOOGLE_HEALTH_CLIENT_SECRET,
        })
        if resp.status_code != 200:
            return RedirectResponse(f"{FRONTEND_URL}/log?error=token_exchange_failed")
        data = resp.json()

    db = await get_db()
    await db.table("user_integrations").upsert({
        "user_id":       user_id,
        "provider":      "fitbit",
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at":    (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))).isoformat(),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id,provider").execute()
    return RedirectResponse(f"{FRONTEND_URL}/log?connected=fitbit")


@router.post("/fitbit/sync")
async def fitbit_sync(user_id: str = Depends(get_user_id)):
    db = await get_db()
    integration = await (
        db.table("user_integrations").select("*")
        .eq("user_id", user_id).eq("provider", "fitbit").single().execute()
    )
    if not integration.data:
        return {"synced": 0, "error": "Fitbit not connected"}

    token   = await _refresh_fitbit_token_if_needed(db, integration.data)
    after   = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        ex_resp = await client.get(
            f"{GOOGLE_HEALTH_BASE}/users/me/dataTypes/exercise/dataPoints",
            headers=headers,
            params={"filter": f'exercise.interval.startTime >= "{after}"', "pageSize": 100},
        )
        sl_resp = await client.get(
            f"{GOOGLE_HEALTH_BASE}/users/me/dataTypes/sleep/dataPoints",
            headers=headers,
            params={"filter": f'sleep.interval.startTime >= "{after}"', "pageSize": 100},
        )

    exercises = ex_resp.json().get("dataPoints", []) if ex_resp.status_code == 200 else []
    sleeps    = sl_resp.json().get("dataPoints", []) if sl_resp.status_code == 200 else []

    rows: list[dict] = []

    for pt in exercises:
        ex  = (pt.get("data") or {}).get("exercise", {})
        ivl = ex.get("interval", {})
        start = _parse_google_time(ivl.get("startTime"))
        if not start:
            continue
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError:
            continue

        dur_min = None
        active  = ex.get("activeDuration", "")
        if isinstance(active, str) and active.endswith("s"):
            try:
                dur_min = round(float(active[:-1]) / 60, 1)
            except ValueError:
                pass
        if dur_min is None:
            end_str = _parse_google_time(ivl.get("endTime"))
            if end_str:
                try:
                    dur_min = round((datetime.fromisoformat(end_str) - start_dt).total_seconds() / 60, 1)
                except ValueError:
                    pass

        metrics = ex.get("metricsSummary", {})
        dist_mm = metrics.get("distanceMillimeters")
        rows.append({
            "user_id":          user_id,
            "type":             "workout",
            "device":           "fitbit",
            "timestamp":        start_dt.isoformat(),
            "workout_type":     GOOGLE_EXERCISE_TYPE_MAP.get(ex.get("exerciseType", ""), "other"),
            "duration_minutes": dur_min,
            "distance_meters":  round(float(dist_mm) / 1000, 1) if dist_mm else None,
            "calories_burned":  metrics.get("caloriesKcal"),
            "avg_heart_rate":   metrics.get("averageHeartRateBeatsPerMinute"),
        })

    for pt in sleeps:
        sl    = (pt.get("data") or {}).get("sleep", {})
        ivl   = sl.get("interval", {})
        start = _parse_google_time(ivl.get("startTime"))
        end   = _parse_google_time(ivl.get("endTime"))
        if not start:
            continue
        dur_hrs = None
        if end:
            try:
                s = datetime.fromisoformat(start)
                e = datetime.fromisoformat(end)
                dur_hrs = round((e - s).total_seconds() / 3600, 2)
            except ValueError:
                pass
        rows.append({
            "user_id":     user_id,
            "type":        "reading",
            "device":      "fitbit",
            "timestamp":   start,
            "sleep_hours": dur_hrs,
        })

    if rows:
        await db.table("watch_data").insert(rows).execute()
    return {
        "synced":       len([r for r in rows if r["type"] == "workout"]),
        "sleep_synced": len([r for r in rows if r["type"] == "reading"]),
    }


async def _refresh_fitbit_token_if_needed(db, integration: dict) -> str:
    expires_at = datetime.fromisoformat(integration["expires_at"])
    if datetime.now(timezone.utc) < expires_at:
        return integration["access_token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": integration["refresh_token"],
            "client_id":     GOOGLE_HEALTH_CLIENT_ID,
            "client_secret": GOOGLE_HEALTH_CLIENT_SECRET,
        })
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to refresh Fitbit token")

    data = resp.json()
    await (
        db.table("user_integrations").update({
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", integration["refresh_token"]),
            "expires_at":    (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))).isoformat(),
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }).eq("id", integration["id"]).execute()
    )
    return data["access_token"]


# ── Garmin (GPX / TCX file import) ───────────────────────────────────────────

@router.post("/garmin/import")
async def garmin_import(file: UploadFile = File(...), user_id: str = Depends(get_user_id)):
    content  = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".gpx"):
        workout_rows, route_rows = _parse_gpx(content, user_id)
    elif filename.endswith(".tcx"):
        workout_rows, route_rows = _parse_tcx(content, user_id)
    else:
        return {"error": "Upload a .gpx or .tcx file exported from Garmin Connect.", "imported": 0, "routes_saved": 0}

    db = await get_db()
    if workout_rows:
        await db.table("watch_data").insert(workout_rows).execute()
    if route_rows:
        await db.table("routes").insert(route_rows).execute()
    return {"imported": len(workout_rows), "routes_saved": len(route_rows)}


def _parse_gpx(content: bytes, user_id: str) -> tuple[list, list]:
    try:
        root = _strip_ns(ET.fromstring(content))
    except ET.ParseError:
        return [], []

    workout_rows, route_rows = [], []

    for trk in root.findall(".//trk"):
        name_el = trk.find("name")
        name    = name_el.text if name_el is not None else None
        type_el = trk.find("type")
        type_hint = (type_el.text if type_el is not None else name) or ""
        workout_type = _classify_activity(type_hint)

        coords, hr_vals = [], []
        for pt in trk.findall(".//trkpt"):
            lat, lon = pt.get("lat"), pt.get("lon")
            time_el  = pt.find("time")
            if lat is None or lon is None or time_el is None:
                continue
            coord: dict = {"lat": float(lat), "lng": float(lon), "timestamp": time_el.text}
            ele_el = pt.find("ele")
            if ele_el is not None:
                try:
                    coord["altitude"] = float(ele_el.text)
                except (ValueError, TypeError):
                    pass
            hr_el = pt.find(".//hr") or pt.find(".//HeartRateBpm/Value")
            if hr_el is not None and hr_el.text:
                try:
                    v = int(hr_el.text)
                    hr_vals.append(v)
                    coord["heart_rate"] = v
                except (ValueError, TypeError):
                    pass
            coords.append(coord)

        if len(coords) < 2:
            continue

        try:
            start_dt = datetime.fromisoformat(coords[0]["timestamp"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(coords[-1]["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue

        duration_s = int((end_dt - start_dt).total_seconds())
        dist_m     = sum(_haversine(coords[i-1]["lat"], coords[i-1]["lng"],
                                    coords[i]["lat"],   coords[i]["lng"])
                         for i in range(1, len(coords)))

        workout_rows.append({
            "user_id":            user_id, "type": "workout", "device": "garmin",
            "timestamp":          start_dt.isoformat(),
            "workout_type":       workout_type,
            "duration_minutes":   round(duration_s / 60, 1),
            "distance_meters":    round(dist_m, 1),
            "avg_heart_rate":     round(sum(hr_vals) / len(hr_vals)) if hr_vals else None,
            "max_heart_rate":     max(hr_vals) if hr_vals else None,
            "ending_heart_rate":  hr_vals[-1]  if hr_vals else None,
            "notes":              name,
        })
        route_rows.append({
            "user_id": user_id, "workout_type": workout_type,
            "coordinates": coords, "distance_meters": round(dist_m, 1),
            "duration_seconds": duration_s,
            "started_at": start_dt.isoformat(), "ended_at": end_dt.isoformat(),
            "notes": name,
        })

    return workout_rows, route_rows


def _parse_tcx(content: bytes, user_id: str) -> tuple[list, list]:
    try:
        root = _strip_ns(ET.fromstring(content))
    except ET.ParseError:
        return [], []

    workout_rows, route_rows = [], []

    for activity in root.findall(".//Activity"):
        sport    = activity.get("Sport", "other")
        workout_type = _classify_activity(sport)
        id_el    = activity.find("Id")
        if id_el is None:
            continue
        try:
            start_dt = datetime.fromisoformat(id_el.text.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        total_time_s = 0
        total_dist_m = 0.0
        total_cal    = 0
        hr_vals, coords = [], []

        for lap in activity.findall(".//Lap"):
            for tag, target in [("TotalTimeSeconds", None), ("DistanceMeters", None),
                                  ("Calories", None)]:
                el = lap.find(tag)
                if el is not None and el.text:
                    try:
                        val = float(el.text)
                        if tag == "TotalTimeSeconds": total_time_s += int(val)
                        elif tag == "DistanceMeters": total_dist_m += val
                        elif tag == "Calories":       total_cal    += int(val)
                    except (ValueError, TypeError):
                        pass

            for tp in lap.findall(".//Trackpoint"):
                time_el = tp.find("Time")
                pos_el  = tp.find("Position")
                hr_el   = tp.find(".//HeartRateBpm/Value") or tp.find("HeartRateBpm/Value")
                if pos_el is not None and time_el is not None:
                    lat_el = pos_el.find("LatitudeDegrees")
                    lng_el = pos_el.find("LongitudeDegrees")
                    if lat_el is not None and lng_el is not None:
                        try:
                            coord: dict = {
                                "lat": float(lat_el.text),
                                "lng": float(lng_el.text),
                                "timestamp": time_el.text,
                            }
                            if hr_el is not None and hr_el.text:
                                v = int(hr_el.text)
                                hr_vals.append(v)
                                coord["heart_rate"] = v
                            coords.append(coord)
                        except (ValueError, TypeError):
                            pass

        end_dt = start_dt + timedelta(seconds=total_time_s)
        workout_rows.append({
            "user_id":            user_id, "type": "workout", "device": "garmin",
            "timestamp":          start_dt.isoformat(),
            "workout_type":       workout_type,
            "duration_minutes":   round(total_time_s / 60, 1),
            "distance_meters":    round(total_dist_m, 1) if total_dist_m else None,
            "calories_burned":    total_cal if total_cal else None,
            "avg_heart_rate":     round(sum(hr_vals) / len(hr_vals)) if hr_vals else None,
            "max_heart_rate":     max(hr_vals) if hr_vals else None,
            "ending_heart_rate":  hr_vals[-1]  if hr_vals else None,
        })
        if len(coords) >= 2:
            route_rows.append({
                "user_id": user_id, "workout_type": workout_type,
                "coordinates": coords,
                "distance_meters":  round(total_dist_m, 1) if total_dist_m else None,
                "duration_seconds": total_time_s,
                "started_at": start_dt.isoformat(), "ended_at": end_dt.isoformat(),
            })

    return workout_rows, route_rows


# ── Apple Health (XML / ZIP export) ──────────────────────────────────────────

APPLE_WORKOUT_MAP = {
    "HKWorkoutActivityTypeRunning":                       "running",
    "HKWorkoutActivityTypeCycling":                       "cycling",
    "HKWorkoutActivityTypeWalking":                       "walking",
    "HKWorkoutActivityTypeHiking":                        "hiking",
    "HKWorkoutActivityTypeSwimming":                      "swimming",
    "HKWorkoutActivityTypeTraditionalStrengthTraining":   "weightlifting",
    "HKWorkoutActivityTypeFunctionalStrengthTraining":    "weightlifting",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "other",
    "HKWorkoutActivityTypeYoga":                          "other",
    "HKWorkoutActivityTypeCrossTraining":                 "other",
    "HKWorkoutActivityTypeElliptical":                    "other",
    "HKWorkoutActivityTypePilates":                       "other",
}


@router.post("/apple/import")
async def apple_import(file: UploadFile = File(...), user_id: str = Depends(get_user_id)):
    content  = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                xml_name = next(
                    (n for n in zf.namelist() if n.lower().endswith("export.xml")), None
                )
                if xml_name is None:
                    return {"error": "No export.xml found in ZIP.", "imported": 0}
                xml_bytes = zf.read(xml_name)
        except zipfile.BadZipFile:
            return {"error": "Invalid ZIP file.", "imported": 0}
    elif filename.endswith(".xml"):
        xml_bytes = content
    else:
        return {"error": "Upload export.xml or the ZIP from Health app → Export All Health Data.", "imported": 0}

    workout_rows, reading_rows = _parse_apple_health(xml_bytes, user_id)
    db = await get_db()
    if workout_rows:
        await db.table("watch_data").insert(workout_rows).execute()
    if reading_rows:
        await db.table("watch_data").insert(reading_rows).execute()
    return {"imported": len(workout_rows), "readings_synced": len(reading_rows)}


def _parse_apple_health(xml_bytes: bytes, user_id: str) -> tuple[list, list]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [], []

    workout_rows: list[dict] = []
    hr_by_day:    dict[str, list[float]] = {}
    steps_by_day: dict[str, int]         = {}
    sleep_by_day: dict[str, float]       = {}

    for w in root.findall("Workout"):
        activity_type = w.get("workoutActivityType", "")
        start_str     = w.get("startDate", "")
        end_str       = w.get("endDate", "")
        if not start_str:
            continue
        try:
            start_dt = _parse_apple_date(start_str)
            end_dt   = _parse_apple_date(end_str) if end_str else start_dt
        except ValueError:
            continue

        duration_s = int((end_dt - start_dt).total_seconds())
        calories = avg_hr = max_hr = dist_m = None

        for stat in w.findall("WorkoutStatistics"):
            stype = stat.get("type", "")
            if "ActiveEnergyBurned" in stype:
                try:
                    calories = float(stat.get("sum") or stat.get("average") or 0) or None
                except (ValueError, TypeError):
                    pass
            elif stype == "HKQuantityTypeIdentifierHeartRate":
                try:
                    avg_hr = round(float(stat.get("average") or 0)) or None
                    max_hr = round(float(stat.get("maximum") or 0)) or None
                except (ValueError, TypeError):
                    pass
            elif "Distance" in stype:
                try:
                    val  = float(stat.get("sum") or 0)
                    unit = stat.get("unit", "km").lower()
                    dist_m = round(val * (1000 if "km" in unit else 1609.34 if "mi" in unit else 1), 1) or None
                except (ValueError, TypeError):
                    pass

        workout_rows.append({
            "user_id":           user_id, "type": "workout", "device": "apple_health",
            "timestamp":         start_dt.isoformat(),
            "workout_type":      APPLE_WORKOUT_MAP.get(activity_type, "other"),
            "duration_minutes":  round(duration_s / 60, 1),
            "distance_meters":   dist_m,
            "calories_burned":   calories,
            "avg_heart_rate":    avg_hr,
            "max_heart_rate":    max_hr,
        })

    for rec in root.findall("Record"):
        rtype     = rec.get("type", "")
        start_str = rec.get("startDate", "")
        if not start_str:
            continue
        try:
            dt  = _parse_apple_date(start_str)
            day = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
        val = rec.get("value", "")

        if rtype == "HKQuantityTypeIdentifierHeartRate":
            try:
                hr_by_day.setdefault(day, []).append(float(val))
            except (ValueError, TypeError):
                pass
        elif rtype == "HKQuantityTypeIdentifierStepCount":
            try:
                steps_by_day[day] = steps_by_day.get(day, 0) + int(float(val))
            except (ValueError, TypeError):
                pass
        elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and val == "HKCategoryValueSleepAnalysisAsleep":
            end_str2 = rec.get("endDate", "")
            if end_str2:
                try:
                    end_dt2 = _parse_apple_date(end_str2)
                    hours = (end_dt2 - dt).total_seconds() / 3600
                    sleep_by_day[day] = sleep_by_day.get(day, 0) + hours
                except ValueError:
                    pass

    reading_rows: list[dict] = []
    for day in sorted(set(list(hr_by_day) + list(steps_by_day) + list(sleep_by_day))):
        row: dict = {"user_id": user_id, "type": "reading", "device": "apple_health",
                     "timestamp": f"{day}T00:00:00+00:00"}
        hrs = hr_by_day.get(day)
        if hrs:
            row["heart_rate"] = round(sum(hrs) / len(hrs))
        if day in steps_by_day:
            row["steps"] = steps_by_day[day]
        if day in sleep_by_day:
            row["sleep_hours"] = round(sleep_by_day[day], 2)
        reading_rows.append(row)

    return workout_rows, reading_rows


# ── Google Fit (Takeout ZIP import) ──────────────────────────────────────────

@router.post("/google/import")
async def google_fit_import(file: UploadFile = File(...), user_id: str = Depends(get_user_id)):
    """
    Import from Google Takeout → Fit data export.
    Go to takeout.google.com, select only 'Fit', export as ZIP, then upload here.
    """
    content = await file.read()
    if not (file.filename or "").lower().endswith(".zip"):
        return {"error": "Upload the ZIP file downloaded from takeout.google.com.", "imported": 0}

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return {"error": "Invalid ZIP file.", "imported": 0}

    workout_rows: list[dict] = []
    reading_rows: list[dict] = []

    with zf:
        names = zf.namelist()

        # ── Session JSON files (individual activities) ─────────────────
        session_files = [n for n in names
                         if "Activities" in n and n.endswith(".json")]
        for fname in session_files:
            try:
                data = json.loads(zf.read(fname))
            except (json.JSONDecodeError, KeyError):
                continue
            if not isinstance(data, dict):
                continue

            start_str = data.get("startTime") or data.get("start_time_millis")
            end_str   = data.get("endTime")   or data.get("end_time_millis")
            if not start_str:
                continue

            try:
                if isinstance(start_str, (int, float)):
                    start_dt = datetime.fromtimestamp(start_str / 1000, tz=timezone.utc)
                    end_dt   = datetime.fromtimestamp((end_str or start_str) / 1000, tz=timezone.utc)
                else:
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    end_dt   = datetime.fromisoformat((end_str or start_str).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            activity_type = str(data.get("activityType", data.get("activity_type", 0)))
            workout_type  = _google_fit_activity_type(activity_type)
            duration_s    = int((end_dt - start_dt).total_seconds())

            calories = dist_m = avg_hr = None
            for seg in data.get("activitySegment", {}).get("activityConfidence", []):
                pass  # presence only; stats are in aggregate fields below
            try:
                calories = float(data.get("calories") or data.get("caloriesExpended") or 0) or None
            except (ValueError, TypeError):
                pass
            try:
                dist_m = float(data.get("distance") or data.get("distanceMeters") or 0) or None
            except (ValueError, TypeError):
                pass

            # Heart rate from aggregate key
            hr_data = data.get("heartRate") or {}
            try:
                avg_hr = round(float(hr_data.get("avg") or hr_data.get("average") or 0)) or None
            except (ValueError, TypeError):
                pass

            workout_rows.append({
                "user_id":          user_id, "type": "workout", "device": "google_fit",
                "timestamp":        start_dt.isoformat(),
                "workout_type":     workout_type,
                "duration_minutes": round(duration_s / 60, 1),
                "distance_meters":  dist_m,
                "calories_burned":  calories,
                "avg_heart_rate":   avg_hr,
            })

        # ── Daily Summary CSV ──────────────────────────────────────────
        csv_file = next(
            (n for n in names if "Daily" in n and n.endswith(".csv")), None
        )
        if csv_file:
            try:
                text = zf.read(csv_file).decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    day = (row.get("Date") or "").strip()
                    if not day:
                        continue
                    reading: dict = {"user_id": user_id, "type": "reading",
                                     "device": "google_fit",
                                     "timestamp": f"{day}T00:00:00+00:00"}
                    for col, field in [
                        ("Step count",               "steps"),
                        ("Calories (kcal)",          "calories_burned"),
                        ("Average heart rate (bpm)", "heart_rate"),
                        ("Sleep duration",           None),
                    ]:
                        val = (row.get(col) or "").strip()
                        if not val:
                            continue
                        if col == "Sleep duration":
                            try:
                                h, m, s = (val + ":0:0").split(":")[:3]
                                reading["sleep_hours"] = round(int(h) + int(m) / 60 + int(s) / 3600, 2)
                            except (ValueError, TypeError):
                                pass
                        elif field:
                            try:
                                reading[field] = round(float(val), 1)
                            except (ValueError, TypeError):
                                pass
                    reading_rows.append(reading)
            except Exception:
                pass

    db = await get_db()
    if workout_rows:
        await db.table("watch_data").insert(workout_rows).execute()
    if reading_rows:
        await db.table("watch_data").insert(reading_rows).execute()
    return {"imported": len(workout_rows), "readings_synced": len(reading_rows)}


_GOOGLE_ACTIVITY_TYPES: dict[str, str] = {
    "7": "cycling", "11": "cycling",
    "1": "other",    "17": "hiking",
    "37": "running", "38": "running",
    "39": "running", "93": "running",
    "41": "other",   "79": "walking",
    "80": "walking", "45": "other",
    "97": "weightlifting", "20": "other",
    "82": "swimming",
}


def _google_fit_activity_type(code: str) -> str:
    return _GOOGLE_ACTIVITY_TYPES.get(str(code), _classify_activity(code))
