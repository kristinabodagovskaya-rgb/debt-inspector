from pydantic import BaseModel
from datetime import date
from enum import Enum


class EnforcementStatus(str, Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class EnforcementProceeding(BaseModel):
    """Исполнительное производство ФССП."""

    number: str | None = None
    date_opened: date | None = None
    subject: str | None = None
    amount: float | None = None
    department: str | None = None
    bailiff: str | None = None
    status: EnforcementStatus = EnforcementStatus.UNKNOWN
    termination_reason: str | None = None
    claimant: str | None = None
    source_url: str | None = None
