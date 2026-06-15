"""Server-side calorie estimation.

Keeps the watch firmware thin: the device no longer hardcodes a body weight or
runs MET math (the ESP32-C3 has no FPU). The watch sends duration + workout type
and the backend estimates the burn here, in one place.

Formula: calories ≈ MET × weight_kg × hours. MET ("metabolic equivalent") values
are moderate-intensity averages from the Compendium of Physical Activities.
"""
from typing import Optional

# TODO: replace with the authenticated user's real weight once a profile exists.
DEFAULT_WEIGHT_KG = 70.0

# MET by activity. Keys are lowercased; the watch currently reports "running".
_MET = {
    "running":       9.8,
    "run":           9.8,
    "jogging":       7.0,
    "walking":       3.5,
    "walk":          3.5,
    "hiking":        6.0,
    "cycling":       7.5,
    "bike":          7.5,
    "biking":        7.5,
    "swimming":      8.0,
    "rowing":        7.0,
    "elliptical":    5.0,
    "weightlifting": 6.0,
    "strength":      6.0,
    "workout":       7.0,
}
_DEFAULT_MET = 7.0


def met_for(workout_type: Optional[str]) -> float:
    if not workout_type:
        return _DEFAULT_MET
    return _MET.get(workout_type.strip().lower(), _DEFAULT_MET)


def estimate_calories(duration_minutes: float,
                      workout_type: Optional[str] = None,
                      weight_kg: float = DEFAULT_WEIGHT_KG) -> float:
    """Estimate kcal burned for a workout, rounded to 1 decimal place."""
    if not duration_minutes or duration_minutes <= 0:
        return 0.0
    return round(met_for(workout_type) * weight_kg * (duration_minutes / 60.0), 1)
