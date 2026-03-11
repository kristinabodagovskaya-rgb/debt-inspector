"""
Простая авторизация по логину и паролю.

Сессия сохраняется в ~/.debt-inspector/session — после успешного логина
повторный ввод не требуется до выхода (logout) или истечения 24 часов.
"""

import hashlib
import json
import time
from pathlib import Path

SESSION_DIR = Path.home() / ".debt-inspector"
SESSION_FILE = SESSION_DIR / "session"
CREDENTIALS_FILE = SESSION_DIR / "credentials.json"
SESSION_TTL = 86400  # 24 часа


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _default_credentials() -> dict:
    """Учётные данные по умолчанию."""
    return {
        "login": _hash("inspector"),
        "password": _hash("debt2026"),
    }


def _load_credentials() -> dict:
    """Загружает credentials (или создаёт дефолтные)."""
    if CREDENTIALS_FILE.exists():
        return json.loads(CREDENTIALS_FILE.read_text())
    return _default_credentials()


def setup_credentials(login: str, password: str):
    """Устанавливает новые логин и пароль."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    creds = {"login": _hash(login), "password": _hash(password)}
    CREDENTIALS_FILE.write_text(json.dumps(creds))


def check_credentials(login: str, password: str) -> bool:
    """Проверяет логин и пароль."""
    creds = _load_credentials()
    return creds["login"] == _hash(login) and creds["password"] == _hash(password)


def save_session():
    """Сохраняет сессию после успешного логина."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(str(int(time.time())))


def is_session_valid() -> bool:
    """Проверяет, есть ли активная сессия."""
    if not SESSION_FILE.exists():
        return False
    try:
        ts = int(SESSION_FILE.read_text().strip())
        return (time.time() - ts) < SESSION_TTL
    except (ValueError, OSError):
        return False


def clear_session():
    """Удаляет сессию (logout)."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
