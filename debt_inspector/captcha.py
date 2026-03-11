"""
Решение капчи через rucaptcha.com / anti-captcha.com.

Оба сервиса совместимы по API (rucaptcha = форк anti-captcha).
Ключ берётся из переменной окружения CAPTCHA_API_KEY.
Провайдер: CAPTCHA_PROVIDER (rucaptcha | anticaptcha), по умолчанию rucaptcha.
"""

import os
import asyncio
import base64
import re

import httpx

PROVIDERS = {
    "rucaptcha": "https://rucaptcha.com",
    "anticaptcha": "https://api.anti-captcha.com",
}

MAX_ATTEMPTS = 30
POLL_INTERVAL = 3  # секунд


class CaptchaSolver:
    def __init__(self):
        self.api_key = os.getenv("CAPTCHA_API_KEY", "")
        provider = os.getenv("CAPTCHA_PROVIDER", "rucaptcha").lower()
        self.base_url = PROVIDERS.get(provider, PROVIDERS["rucaptcha"])
        self.client = httpx.AsyncClient(timeout=30.0)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def close(self):
        await self.client.aclose()

    async def solve_image(self, image_data: bytes) -> str | None:
        """Решает капчу из изображения (PNG/JPG bytes). Возвращает текст."""
        if not self.is_configured:
            return None

        b64 = base64.b64encode(image_data).decode()

        # Отправка задачи
        resp = await self.client.post(
            f"{self.base_url}/in.php",
            data={
                "key": self.api_key,
                "method": "base64",
                "body": b64,
                "json": "1",
            },
        )
        result = resp.json()

        if result.get("status") != 1:
            return None

        task_id = result["request"]

        # Ожидание результата
        for _ in range(MAX_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)

            resp = await self.client.get(
                f"{self.base_url}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": "1",
                },
            )
            result = resp.json()

            if result.get("status") == 1:
                return result["request"]

            if result.get("request") == "CAPCHA_NOT_READY":
                continue

            # Ошибка
            return None

        return None

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str | None:
        """Решает reCAPTCHA v2. Возвращает g-recaptcha-response токен."""
        if not self.is_configured:
            return None

        resp = await self.client.post(
            f"{self.base_url}/in.php",
            data={
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": "1",
            },
        )
        result = resp.json()

        if result.get("status") != 1:
            return None

        task_id = result["request"]

        for _ in range(MAX_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)

            resp = await self.client.get(
                f"{self.base_url}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": "1",
                },
            )
            result = resp.json()

            if result.get("status") == 1:
                return result["request"]

            if result.get("request") == "CAPCHA_NOT_READY":
                continue

            return None

        return None


def extract_captcha_image_url(html: str) -> str | None:
    """Извлекает URL картинки капчи из HTML."""
    match = re.search(r'<img[^>]*captcha[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r'src=["\']([^"\']*captcha[^"\']*)["\']', html, re.IGNORECASE)
    if match:
        return match.group(1)

    return None


def extract_recaptcha_sitekey(html: str) -> str | None:
    """Извлекает sitekey reCAPTCHA v2 из HTML."""
    match = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)

    match = re.search(r"sitekey['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    if match:
        return match.group(1)

    return None
