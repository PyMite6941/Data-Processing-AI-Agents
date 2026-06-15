import asyncio
import csv
import json
import os
import tempfile
from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone
from auth import get_user_id
from db import get_db
from models.analysis import AnalysisRequest
from bots import Bots

router = APIRouter()

# Columns from the routes table that are meaningful for analysis.
# Excludes coordinates (raw lat/lng blob), user_id, and id.
_ROUTE_COLUMNS = [
    "started_at", "ended_at", "workout_type",
    "distance_meters", "duration_seconds", "pace", "calories_burned", "notes",
]


def _write_csv(rows: list[dict], columns: list[str]) -> str:
    """Write rows to a temp CSV (only the given columns) and return the path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return f.name


@router.post("/")
async def run_analysis(request: AnalysisRequest, user_id: str = Depends(get_user_id)):
    """Pull the user's watch data + routes, run the AI crew, store and return the result."""
    db = await get_db()

    # ── Watch readings + workouts ──────────────────────────────────────────
    wd_query = db.table("watch_data").select("*").eq("user_id", user_id)
    if request.date_from:
        wd_query = wd_query.gte("timestamp", request.date_from.isoformat())
    if request.date_to:
        wd_query = wd_query.lte("timestamp", request.date_to.isoformat())

    wd_result = await wd_query.order("timestamp").execute()
    wd_rows = wd_result.data

    if not wd_rows:
        raise HTTPException(status_code=404, detail="No watch data found for this user")

    for row in wd_rows:
        row.pop("user_id", None)

    # ── GPS routes ─────────────────────────────────────────────────────────
    rt_query = db.table("routes").select(", ".join(_ROUTE_COLUMNS)).eq("user_id", user_id)
    if request.date_from:
        rt_query = rt_query.gte("started_at", request.date_from.isoformat())
    if request.date_to:
        rt_query = rt_query.lte("started_at", request.date_to.isoformat())

    rt_result = await rt_query.order("started_at").execute()
    rt_rows = rt_result.data

    # ── Write CSVs ─────────────────────────────────────────────────────────
    tmp_watch = _write_csv(wd_rows, list(wd_rows[0].keys()))
    tmp_routes = _write_csv(rt_rows, _ROUTE_COLUMNS) if rt_rows else None

    # Pass both paths (newline-separated) — the crew's task descriptions already
    # handle multiple files and say "If multiple files, analyze together."
    data_input = tmp_watch
    if tmp_routes:
        data_input = f"{tmp_watch}\n{tmp_routes}"

    try:
        bots = Bots(context=request.context)
        loop = asyncio.get_running_loop()
        raw_json = await loop.run_in_executor(None, lambda: bots.create_crew(data=data_input))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")
    finally:
        if os.path.exists(tmp_watch):
            os.unlink(tmp_watch)
        if tmp_routes and os.path.exists(tmp_routes):
            os.unlink(tmp_routes)

    # Parse the structured FormattedOutput returned by bots.create_crew()
    parsed: dict = {}
    try:
        parsed = json.loads(raw_json)
    except Exception:
        parsed = {}

    record = {
        "user_id": user_id,
        "context": request.context,
        "summary": parsed.get("summary") or raw_json,
        "key_findings": parsed.get("findings") or [],
        "recommendations": parsed.get("recommendations") or [],
        "output_type": parsed.get("output_type"),
        "chart_type": parsed.get("chart_type"),
        "chart_title": parsed.get("chart_title"),
        "data_points": parsed.get("data_points"),
        "metrics": parsed.get("metrics"),
        "table_headers": parsed.get("table_headers"),
        "table_rows": parsed.get("table_rows"),
        "quality_score": parsed.get("quality_score"),
        "quality_verdict": parsed.get("quality_verdict"),
        "raw_output": parsed or None,   # full payload — lets frontend render comparison/heatmap/code
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # output_type must always be present so the frontend can pick the right renderer
    if not record.get("output_type"):
        record["output_type"] = "report"

    # Strip remaining None values so Supabase doesn't complain about unknown columns
    record = {k: v for k, v in record.items() if v is not None}

    await db.table("analyses").insert(record).execute()
    return record
