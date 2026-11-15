"""Read-only scheduler status and reviewed official NSE calendar import API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.connection import get_db
from scheduler.calendar import import_official_calendar_csv
from scheduler.service import scheduler_status

router = APIRouter(prefix="/scheduler", tags=["daily-automation"])


class CalendarImportRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=200_000)
    source_url: str = Field(min_length=10, max_length=500)
    source_name: str = Field(min_length=1, max_length=160)


@router.get("/status")
def get_scheduler_status() -> dict:
    """Return this backend process state and its durable run history."""
    return scheduler_status()


@router.post("/calendar/import")
def import_calendar(request: CalendarImportRequest, session: Session = Depends(get_db)) -> dict:
    """Import a year of official NSE holidays and special-session exceptions."""
    try:
        year = import_official_calendar_csv(session, request.csv_text,
                                            source_url=request.source_url,
                                            source_name=request.source_name)
        return {"year": year, "status": "imported"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
