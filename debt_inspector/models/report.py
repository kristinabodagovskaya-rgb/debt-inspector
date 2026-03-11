from pydantic import BaseModel, Field
from datetime import datetime

from .debtor import SearchParams, DebtorInfo
from .enforcement import EnforcementProceeding
from .court_case import CourtCase
from .bankruptcy import BankruptcyCase


class DebtReport(BaseModel):
    """Полный отчёт по результатам проверки."""

    search_params: SearchParams
    debtor_info: DebtorInfo | None = None
    enforcements: list[EnforcementProceeding] = Field(default_factory=list)
    court_cases: list[CourtCase] = Field(default_factory=list)
    bankruptcies: list[BankruptcyCase] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=datetime.now)

    @property
    def total_enforcement_debt(self) -> float:
        return sum(e.amount or 0 for e in self.enforcements)

    @property
    def total_court_claims(self) -> float:
        return sum(c.amount or 0 for c in self.court_cases)

    @property
    def has_active_bankruptcy(self) -> bool:
        return any(
            b.phase not in ("completed", "unknown")
            for b in self.bankruptcies
        )

    def summary(self) -> dict:
        return {
            "debtor": self.search_params.display_name,
            "checked_at": self.checked_at.isoformat(),
            "enforcements_count": len(self.enforcements),
            "enforcement_debt_total": self.total_enforcement_debt,
            "court_cases_count": len(self.court_cases),
            "court_claims_total": self.total_court_claims,
            "bankruptcies_count": len(self.bankruptcies),
            "has_active_bankruptcy": self.has_active_bankruptcy,
            "errors": self.errors,
        }
