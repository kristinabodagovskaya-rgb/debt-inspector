"""
Парсер ФССП (Федеральная служба судебных приставов).

Рабочий endpoint: https://is-go.fssp.gov.ru/ajax_search (GET, JSONP).
Капча показывается пользователю в браузере — он вводит код вручную.
"""

import asyncio
import json
import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus

SEARCH_URL = "https://is-go.fssp.gov.ru/ajax_search"


class CaptchaRequired(Exception):
    """Исключение — нужна капча. Содержит данные для показа пользователю."""

    def __init__(self, captcha_image: str, code_id: str, query_params: dict):
        self.captcha_image = captcha_image  # base64 PNG
        self.code_id = code_id
        self.query_params = query_params
        super().__init__("ФССП требует ввод капчи")


class FSSPSource(BaseSource):
    name = "fssp"
    base_url = "https://fssp.gov.ru"

    async def search(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Поиск исполнительных производств. Может выбросить CaptchaRequired."""
        try:
            await self._get(f"{self.base_url}/iss/ip")
            await asyncio.sleep(0.5)

            query_params = self._build_params(params)
            if not query_params:
                return []

            return await self._do_search(query_params)
        except CaptchaRequired:
            raise  # Пробрасываем наверх для показа пользователю
        except Exception as e:
            raise RuntimeError(f"ФССП: {e}") from e

    async def search_with_captcha(
        self, query_params: dict, code_id: str, captcha_code: str
    ) -> list[EnforcementProceeding]:
        """Повторный запрос с решённой капчей."""
        await self._get(f"{self.base_url}/iss/ip")
        await asyncio.sleep(0.3)

        callback_name = "jsonp_cb"
        params = {**query_params}
        params["code_id"] = code_id
        params["code"] = captcha_code
        params["callback"] = callback_name

        resp = await self._get(SEARCH_URL, params=params)
        data = self._parse_jsonp(resp.text, callback_name)

        if not data:
            return []

        html = data.get("data", "")

        if "captcha-popup" in html:
            # Капча снова — неправильный код
            img, new_code_id = self._extract_captcha(html)
            raise CaptchaRequired(img, new_code_id, query_params)

        return self._parse_results(html)

    def _build_params(self, params: SearchParams) -> dict | None:
        """Формирует параметры запроса."""
        base = {
            "system": "ip",
            "nocache": "1",
            "is[extended]": "1",
        }

        if params.region:
            base["is[region_id][0]"] = str(params.region)

        if params.inn:
            return {**base, "is[variant]": "5", "is[inn]": params.inn}

        if params.subject_type == SubjectType.PERSON:
            if not params.last_name:
                return None
            result = {
                **base,
                "is[variant]": "1",
                "is[last_name]": params.last_name or "",
                "is[first_name]": params.first_name or "",
                "is[patronymic]": params.middle_name or "",
            }
            if params.birth_date:
                result["is[date]"] = params.birth_date
            return result

        if params.company_name:
            return {
                **base,
                "is[variant]": "2",
                "is[drtr_name]": params.company_name,
            }

        return None

    async def _do_search(self, query_params: dict) -> list[EnforcementProceeding]:
        """Выполняет запрос. При капче — выбрасывает CaptchaRequired."""
        callback_name = "jsonp_cb"
        query_params["callback"] = callback_name

        resp = await self._get(SEARCH_URL, params=query_params)
        data = self._parse_jsonp(resp.text, callback_name)

        if not data:
            return []

        html = data.get("data", "")

        if not html or "Заполните" in html or "Выберите" in html or len(html) < 50:
            return []

        if "captcha-popup" in html:
            img, code_id = self._extract_captcha(html)
            # Убираем callback из params перед сохранением
            saved_params = {k: v for k, v in query_params.items() if k != "callback"}
            raise CaptchaRequired(img, code_id, saved_params)

        return self._parse_results(html)

    def _extract_captcha(self, html: str) -> tuple[str, str]:
        """Извлекает base64 картинку и code_id из HTML капчи."""
        img_match = re.search(r'src="data:image/png;base64,([^"]+)"', html)
        captcha_image = img_match.group(1) if img_match else ""

        code_id_match = re.search(r'code_id=([^&"]+)', html)
        code_id = code_id_match.group(1) if code_id_match else ""

        return captcha_image, code_id

    def _parse_jsonp(self, text: str, callback_name: str) -> dict | None:
        """Парсит JSONP ответ."""
        text = text.strip()
        prefix = f"{callback_name}("
        if text.startswith(prefix) and text.endswith(");"):
            json_str = text[len(prefix):-2]
        elif text.startswith(prefix) and text.endswith(")"):
            json_str = text[len(prefix):-1]
        else:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
            else:
                return None

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _parse_results(self, html: str) -> list[EnforcementProceeding]:
        """Парсинг HTML результатов."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Таблица
        table = soup.select_one("table") or soup.select_one(".results")
        if table:
            rows = table.select("tr")
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 4:
                    proc = self._parse_row(cells)
                    if proc:
                        results.append(proc)

        # Блоки
        if not results:
            for block in soup.select(".iss-result") or soup.select("[class*='result']"):
                proc = self._parse_block(block)
                if proc:
                    results.append(proc)

        # Raw text
        if not results and len(html) > 200:
            results = self._parse_text(html)

        return results

    def _parse_row(self, cells: list) -> EnforcementProceeding | None:
        try:
            texts = [c.get_text(strip=True) for c in cells]
            proc = EnforcementProceeding()

            for text in texts:
                ip_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
                if ip_match and not proc.number:
                    proc.number = ip_match.group(1)

                amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})", text)
                if amount_match and not proc.amount:
                    s = amount_match.group(1).replace(" ", "").replace(",", ".")
                    try:
                        proc.amount = float(s)
                    except ValueError:
                        pass

                date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
                if date_match and not proc.date_opened:
                    try:
                        proc.date_opened = date(
                            *map(int, reversed(date_match.group(1).split(".")))
                        )
                    except (ValueError, TypeError):
                        pass

            longest = max(texts, key=len) if texts else ""
            if len(longest) > 20:
                proc.subject = longest[:200]

            if proc.number:
                full_text = " ".join(texts).lower()
                if "окончено" in full_text or "исполнено" in full_text:
                    proc.status = EnforcementStatus.FINISHED
                elif "приостановлено" in full_text:
                    proc.status = EnforcementStatus.SUSPENDED
                else:
                    proc.status = EnforcementStatus.ACTIVE
                return proc
        except (IndexError, AttributeError):
            pass
        return None

    def _parse_block(self, block) -> EnforcementProceeding | None:
        text = block.get_text(separator="\n", strip=True)
        if len(text) < 20:
            return None
        return self._extract_from_text(text)

    def _parse_text(self, html: str) -> list[EnforcementProceeding]:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        results = []

        for ip_num in re.findall(r"(\d+/\d+/\d+-\w+)", text):
            idx = text.find(ip_num)
            context = text[max(0, idx - 200):idx + 500]
            proc = self._extract_from_text(context)
            if proc:
                proc.number = ip_num
                results.append(proc)

        return results

    def _extract_from_text(self, text: str) -> EnforcementProceeding | None:
        proc = EnforcementProceeding()

        num_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
        if num_match:
            proc.number = num_match.group(1)

        amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})\s*(?:руб|₽)?", text)
        if amount_match:
            s = amount_match.group(1).replace(" ", "").replace(",", ".")
            try:
                proc.amount = float(s)
            except ValueError:
                pass

        date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
        if date_match:
            try:
                proc.date_opened = date(
                    *map(int, reversed(date_match.group(1).split(".")))
                )
            except (ValueError, TypeError):
                pass

        subj_match = re.search(r"(?:Предмет|предмет)[:\s]*(.+?)(?:\n|$)", text)
        if subj_match:
            proc.subject = subj_match.group(1).strip()[:200]

        lower = text.lower()
        if "окончено" in lower or "исполнено" in lower:
            proc.status = EnforcementStatus.FINISHED
        elif "приостановлено" in lower:
            proc.status = EnforcementStatus.SUSPENDED
        elif proc.number:
            proc.status = EnforcementStatus.ACTIVE

        return proc if proc.number else None
