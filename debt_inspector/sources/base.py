import abc
import os
import httpx
from fake_useragent import UserAgent
from tenacity import retry, stop_after_attempt, wait_exponential

from debt_inspector.models.debtor import SearchParams

ua = UserAgent()


def _get_proxy() -> str | None:
    """
    Прокси для доступа к российским госсайтам.
    Установите PROXY_URL, например:
      PROXY_URL=socks5://user:pass@ip:port
      PROXY_URL=http://ip:port
    """
    return os.getenv("PROXY_URL")


class BaseSource(abc.ABC):
    """Базовый класс для всех источников данных."""

    name: str = "base"
    base_url: str = ""
    use_proxy: bool = True

    def __init__(self):
        proxy = _get_proxy() if self.use_proxy else None
        self.client = httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": ua.random},
            verify=False,
            proxy=proxy,
        )

    async def close(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=15))
    async def _get(self, url: str, **kwargs) -> httpx.Response:
        self.client.headers["User-Agent"] = ua.random
        resp = await self.client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=15))
    async def _post(self, url: str, **kwargs) -> httpx.Response:
        self.client.headers["User-Agent"] = ua.random
        resp = await self.client.post(url, **kwargs)
        resp.raise_for_status()
        return resp

    @abc.abstractmethod
    async def search(self, params: SearchParams) -> list:
        """Выполнить поиск по параметрам. Возвращает список моделей."""
        ...
