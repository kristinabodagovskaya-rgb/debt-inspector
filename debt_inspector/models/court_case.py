from pydantic import BaseModel
from datetime import date
from enum import Enum


class CourtType(str, Enum):
    ARBITRATION = "arbitration"
    GENERAL = "general"
    MAGISTRATE = "magistrate"


class CourtCaseStatus(str, Enum):
    ACTIVE = "active"
    DECIDED = "decided"
    APPEALED = "appealed"
    UNKNOWN = "unknown"


class CourtCase(BaseModel):
    """Судебное дело."""

    case_number: str | None = None
    court_name: str | None = None
    court_type: CourtType = CourtType.GENERAL
    judge: str | None = None
    date_filed: date | None = None
    date_decided: date | None = None
    subject: str | None = None
    amount: float | None = None
    status: CourtCaseStatus = CourtCaseStatus.UNKNOWN
    plaintiff: str | None = None
    defendant: str | None = None
    source_url: str | None = None
