from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class WatchReading(BaseModel):
    timestamp: datetime
    heart_rate: Optional[float] = None
    steps: Optional[int] = None
    calories_burned: Optional[float] = None
    active_calories: Optional[float] = None
    distance_meters: Optional[float] = None
    sleep_hours: Optional[float] = None
    spo2: Optional[float] = None       # blood oxygen %
    hrv: Optional[float] = None        # heart rate variability


class WorkoutSession(BaseModel):
    timestamp: datetime
    workout_type: str                  # running, cycling, weightlifting, etc.
    duration_minutes: float
    avg_heart_rate: Optional[float] = None
    max_heart_rate: Optional[float] = None
    ending_heart_rate: Optional[float] = None   # BPM at end/cool-down
    calories_burned: Optional[float] = None
    distance_meters: Optional[float] = None
    notes: Optional[str] = None


class WatchSyncPayload(BaseModel):
    readings: list[WatchReading] = []
    workouts: list[WorkoutSession] = []
    device: Optional[str] = None      # "apple_watch", "garmin", "fitbit", etc.
    app_version: Optional[str] = None
