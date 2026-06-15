from fastapi import APIRouter, Depends, HTTPException
from auth import get_user_id
from db import get_db
from models.watch import WatchSyncPayload
from calories import estimate_calories

router = APIRouter()


@router.post("/sync")
async def sync_watch_data(payload: WatchSyncPayload, user_id: str = Depends(get_user_id)):
    """Receive a batch of watch readings and workouts from the phone/watch app."""
    db = await get_db()
    rows = []

    for reading in payload.readings:
        rows.append({
            "user_id": user_id,
            "type": "reading",
            "device": payload.device,
            **reading.model_dump(),
        })

    # The watch no longer computes calories (it can't know the user's weight), so
    # we estimate them here. Only for watch-originated workouts — manual web
    # entries intentionally leave calories blank unless the user types them.
    is_watch = (payload.device or "").startswith("fitness_watch")

    for workout in payload.workouts:
        row = {
            "user_id": user_id,
            "type": "workout",
            "device": payload.device,
            **workout.model_dump(),
        }
        if is_watch and row.get("calories_burned") is None:
            row["calories_burned"] = estimate_calories(
                row.get("duration_minutes") or 0, row.get("workout_type"))
        rows.append(row)

    if not rows:
        raise HTTPException(status_code=400, detail="No data provided")

    for row in rows:
        if row.get("timestamp"):
            row["timestamp"] = row["timestamp"].isoformat()

    await db.table("watch_data").insert(rows).execute()
    return {"synced": len(rows)}
