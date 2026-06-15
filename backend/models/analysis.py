from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class AnalysisRequest(BaseModel):
    context: str                       # what the user wants to know
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


class AnalysisResult(BaseModel):
    user_id: str
    context: str
    summary: str
    key_findings: list[str]
    anomalies: list[str]
    recommendations: list[str]
    created_at: datetime = datetime.utcnow()
