"""
Проверка налоговых задолженностей через ФНС.

Использует service.nalog.ru для проверки долгов по ИНН.
"""

import re
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams
from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus


class FNSSource(BaseSource):
    name = "fns"
    base_url = "https://service.nalog.ru"
    use_proxy = False  # nalog.ru блокирует иностранные IP, ходим напрямую

    async def search(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Проверка налоговой задолженности по ИНН."""
        if not params.inn:
            return []

        results = []

        try:
            # Метод 1: service.nalog.ru/bi.do — проверка задолженности
            results = await self._check_bi(params.inn)
            if results:
                return results
        except Exception:
            pass

        try:
            # Метод 2: pb.nalog.ru — поиск налогоплательщика
            results = await self._check_pb(params.inn)
        except Exception:
            pass

        return results

    async def _check_bi(self, inn: str) -> list[EnforcementProceeding]:
        """Проверка через service.nalog.ru/bi.do."""
        # Инициализация сессии
        await self._get(f"{self.base_url}/bi.do")

        # Запрос проверки
        resp = await self._post(
            f"{self.base_url}/bi.do",
            data={
                "inn": inn,
                "fam": "",
                "nam": "",
                "otch": "",
                "bdate": "",
                "doctype": "21",
                "docno": "",
            },
        )

        return self._parse_bi_response(resp.text)

    def _parse_bi_response(self, html: str) -> list[EnforcementProceeding]:
        """Парсинг ответа service.nalog.ru/bi.do."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Ищем таблицу с задолженностями
        for table in soup.select("table"):
            rows = table.select("tr")
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 2:
                    text = row.get_text(separator=" ", strip=True)
                    # Ищем суммы
                    amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})", text)
                    if amount_match:
                        s = amount_match.group(1).replace(" ", "").replace(",", ".")
                        try:
                            amount = float(s)
                            if amount > 0:
                                proc = EnforcementProceeding(
                                    subject=f"Налоговая задолженность (ФНС)",
                                    amount=amount,
                                    status=EnforcementStatus.ACTIVE,
                                    claimant="ФНС России",
                                )
                                results.append(proc)
                        except ValueError:
                            pass

        # Ищем текст о задолженности
        if not results:
            text = soup.get_text()
            if "задолженность" in text.lower():
                amounts = re.findall(r"(\d[\d\s]*[.,]\d{2})\s*(?:руб|₽)?", text)
                for amt_str in amounts:
                    s = amt_str.replace(" ", "").replace(",", ".")
                    try:
                        amount = float(s)
                        if amount > 0:
                            results.append(EnforcementProceeding(
                                subject="Налоговая задолженность (ФНС)",
                                amount=amount,
                                status=EnforcementStatus.ACTIVE,
                                claimant="ФНС России",
                            ))
                    except ValueError:
                        pass

        return results

    async def _check_pb(self, inn: str) -> list[EnforcementProceeding]:
        """Проверка через pb.nalog.ru."""
        resp = await self._post(
            "https://pb.nalog.ru/search.html",
            data={"query": inn, "region": "", "page": ""},
        )

        text = resp.text
        results = []

        if "задолженность" in text.lower() or "долг" in text.lower():
            soup = BeautifulSoup(text, "lxml")
            for block in soup.select(".result-item, .debt-item, tr"):
                block_text = block.get_text(separator=" ", strip=True)
                amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})", block_text)
                if amount_match:
                    s = amount_match.group(1).replace(" ", "").replace(",", ".")
                    try:
                        amount = float(s)
                        if amount > 0:
                            results.append(EnforcementProceeding(
                                subject="Налоговая задолженность (ФНС)",
                                amount=amount,
                                status=EnforcementStatus.ACTIVE,
                                claimant="ФНС России",
                            ))
                    except ValueError:
                        pass

        return results
