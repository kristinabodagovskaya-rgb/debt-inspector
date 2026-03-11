"""
Парсер Картотеки арбитражных дел (kad.arbitr.ru).

Используется полуофициальный API, который работает через AJAX-запросы.
Ищет дела по ИНН, наименованию или ФИО участника.
"""

import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.court_case import CourtCase, CourtType, CourtCaseStatus


class KadArbitrSource(BaseSource):
    name = "kad.arbitr"
    base_url = "https://kad.arbitr.ru"

    async def search(self, params: SearchParams) -> list[CourtCase]:
        """Поиск арбитражных дел."""
        try:
            return await self._search_cases(params)
        except Exception as e:
            raise RuntimeError(f"КАД Арбитр: ошибка поиска — {e}") from e

    async def _search_cases(self, params: SearchParams) -> list[CourtCase]:
        """Поиск через AJAX API kad.arbitr.ru."""
        search_url = f"{self.base_url}/Kad/SearchInstances"

        # Подготовка поисковой строки
        if params.inn:
            participant = params.inn
        elif params.subject_type == SubjectType.COMPANY:
            participant = params.company_name or ""
        else:
            participant = params.full_name

        if not participant:
            return []

        # kad.arbitr.ru ожидает особые заголовки
        headers = {
            "Accept": "application/json, text/javascript, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url,
            "Origin": self.base_url,
        }

        # Сначала загружаем главную для cookies
        await self._get(self.base_url)

        payload = {
            "Page": 1,
            "Count": 25,
            "Courts": [],
            "DateFrom": None,
            "DateTo": None,
            "Sides": [
                {
                    "Name": participant,
                    "Type": -1,
                    "ExactMatch": bool(params.inn),
                }
            ],
            "Cases": [],
            "Judges": [],
            "CaseType": None,
        }

        try:
            resp = await self._post(search_url, json=payload, headers=headers)
            data = resp.json()
            return self._parse_api_response(data)
        except Exception:
            # Fallback на HTML
            return await self._search_html(participant)

    def _parse_api_response(self, data: dict) -> list[CourtCase]:
        """Парсинг JSON ответа от kad.arbitr.ru."""
        results = []

        items = data.get("Result", {}).get("Items", [])

        for item in items:
            case = CourtCase(
                court_type=CourtType.ARBITRATION,
            )

            case.case_number = item.get("CaseNumber")
            case.court_name = item.get("CourtName")
            case.judge = item.get("Judge")

            # Дата
            date_str = item.get("Date")
            if date_str:
                try:
                    case.date_filed = date.fromisoformat(date_str[:10])
                except (ValueError, TypeError):
                    pass

            # Стороны
            sides = item.get("Sides", [])
            for side in sides:
                side_type = side.get("Type", 0)
                side_name = side.get("Name", "")
                if side_type == 1:  # Истец
                    case.plaintiff = side_name
                elif side_type == 2:  # Ответчик
                    case.defendant = side_name

            # Статус
            is_finished = item.get("IsFinished", False)
            case.status = (
                CourtCaseStatus.DECIDED if is_finished else CourtCaseStatus.ACTIVE
            )

            # URL
            if case.case_number:
                case.source_url = f"{self.base_url}/Card/{case.case_number}"

            results.append(case)

        return results

    async def _search_html(self, query: str) -> list[CourtCase]:
        """Fallback: парсинг HTML."""
        resp = await self._get(self.base_url, params={"text": query})
        return self._parse_html_results(resp.text)

    def _parse_html_results(self, html: str) -> list[CourtCase]:
        """Парсинг HTML результатов."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        rows = soup.select("#b-cases .b-cases-table tr") or soup.select(
            "[class*='case-item']"
        )

        for row in rows:
            text = row.get_text(separator="\n", strip=True)
            if not text or len(text) < 15:
                continue

            case = CourtCase(court_type=CourtType.ARBITRATION)

            # Номер дела
            case_link = row.select_one("a[href*='Card']")
            if case_link:
                case.case_number = case_link.get_text(strip=True)
                href = case_link.get("href", "")
                case.source_url = (
                    f"{self.base_url}{href}" if href.startswith("/") else href
                )

            # Номер дела из текста
            if not case.case_number:
                case_match = re.search(r"(А\d+-\d+/\d{4})", text)
                if case_match:
                    case.case_number = case_match.group(1)

            # Суд
            court_el = row.select_one("[class*='court']")
            if court_el:
                case.court_name = court_el.get_text(strip=True)

            # Дата
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if date_match:
                try:
                    case.date_filed = date(
                        *map(int, reversed(date_match.group(1).split(".")))
                    )
                except (ValueError, TypeError):
                    pass

            # Сумма
            amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})\s*(?:руб|₽)", text)
            if amount_match:
                amount_str = amount_match.group(1).replace(" ", "").replace(",", ".")
                try:
                    case.amount = float(amount_str)
                except ValueError:
                    pass

            if case.case_number:
                results.append(case)

        return results
