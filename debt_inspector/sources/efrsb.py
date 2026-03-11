"""
Парсер ЕФРСБ (Единый федеральный реестр сведений о банкротстве).

bankrot.fedresurs.ru — поиск по ФИО / ИНН / ОГРН.
Данные отдаются через REST-подобные endpoint'ы (JSON), что упрощает парсинг.
"""

import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.bankruptcy import BankruptcyCase, BankruptcyPhase


PHASE_MAP = {
    "наблюдение": BankruptcyPhase.OBSERVATION,
    "реструктуризация": BankruptcyPhase.RESTRUCTURING,
    "реализация": BankruptcyPhase.SALE,
    "мировое": BankruptcyPhase.SETTLEMENT,
    "завершено": BankruptcyPhase.COMPLETED,
    "прекращено": BankruptcyPhase.COMPLETED,
}


class EFRSBSource(BaseSource):
    name = "efrsb"
    base_url = "https://bankrot.fedresurs.ru"

    async def search(self, params: SearchParams) -> list[BankruptcyCase]:
        """Поиск в реестре банкротств."""
        try:
            # ЕФРСБ имеет внутренний API для поиска
            results = await self._search_api(params)
            if not results:
                results = await self._search_html(params)
            return results
        except Exception as e:
            raise RuntimeError(f"ЕФРСБ: ошибка поиска — {e}") from e

    async def _search_api(self, params: SearchParams) -> list[BankruptcyCase]:
        """Поиск через внутренний API ЕФРСБ."""
        search_url = f"{self.base_url}/backend/ccard/search"

        search_params = {}

        if params.inn:
            search_params["searchString"] = params.inn
        elif params.subject_type == SubjectType.PERSON:
            search_params["searchString"] = params.full_name
        else:
            search_params["searchString"] = params.company_name or ""

        if not search_params.get("searchString"):
            return []

        search_params["limit"] = 50
        search_params["offset"] = 0
        search_params["isActiveLegal"] = "null"

        try:
            resp = await self._get(search_url, params=search_params)
            data = resp.json()
        except Exception:
            return []

        results = []
        items = data if isinstance(data, list) else data.get("pageData", [])

        for item in items:
            case = self._parse_api_item(item)
            if case:
                results.append(case)

        return results

    def _parse_api_item(self, item: dict) -> BankruptcyCase | None:
        """Парсинг элемента из API ответа."""
        try:
            case = BankruptcyCase(
                debtor_name=item.get("name") or item.get("fullName"),
                debtor_inn=item.get("inn"),
                debtor_address=item.get("address"),
                case_number=item.get("caseNumber"),
                source_url=f"{self.base_url}/DebtorProfile/{item.get('guid', '')}",
            )

            # Определение фазы
            status = (item.get("statusName") or item.get("status") or "").lower()
            for key, phase in PHASE_MAP.items():
                if key in status:
                    case.phase = phase
                    break

            # Арбитражный управляющий
            manager = item.get("arbitrManager") or {}
            if isinstance(manager, dict):
                case.arbitration_manager = manager.get("name") or manager.get("fullName")
                case.arbitration_manager_inn = manager.get("inn")

            return case
        except Exception:
            return None

    async def _search_html(self, params: SearchParams) -> list[BankruptcyCase]:
        """Fallback: парсинг HTML страницы поиска."""
        search_url = f"{self.base_url}/DebtorsSearch.aspx"

        query = params.inn or params.full_name or params.company_name or ""
        if not query:
            return []

        resp = await self._get(search_url, params={"searchstring": query})
        return self._parse_search_html(resp.text)

    def _parse_search_html(self, html: str) -> list[BankruptcyCase]:
        """Парсинг HTML результатов поиска."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Ищем карточки должников
        cards = soup.select(".search-results .row") or soup.select("[class*='debtor']")

        for card in cards:
            text = card.get_text(separator="\n", strip=True)
            if not text or len(text) < 10:
                continue

            case = BankruptcyCase()

            # Имя
            name_el = card.select_one("a") or card.select_one("[class*='name']")
            if name_el:
                case.debtor_name = name_el.get_text(strip=True)
                href = name_el.get("href", "")
                if href:
                    case.source_url = (
                        f"{self.base_url}{href}" if href.startswith("/") else href
                    )

            # ИНН
            inn_match = re.search(r"ИНН[:\s]*(\d{10,12})", text)
            if inn_match:
                case.debtor_inn = inn_match.group(1)

            # Номер дела
            case_match = re.search(r"(А\d+-\d+/\d{4})", text)
            if case_match:
                case.case_number = case_match.group(1)

            # Фаза
            text_lower = text.lower()
            for key, phase in PHASE_MAP.items():
                if key in text_lower:
                    case.phase = phase
                    break

            # Дата
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if date_match:
                try:
                    case.date_decision = date(
                        *map(int, reversed(date_match.group(1).split(".")))
                    )
                except (ValueError, TypeError):
                    pass

            if case.debtor_name:
                results.append(case)

        return results
