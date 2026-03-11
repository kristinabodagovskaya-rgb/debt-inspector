"""
Кеш результатов в SQLite — для дедупликации и отслеживания изменений.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from debt_inspector.models.report import DebtReport

DEFAULT_DB = Path.home() / ".debt-inspector" / "cache.db"


class ResultCache:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                report_json TEXT NOT NULL,
                enforcements_count INTEGER DEFAULT 0,
                court_cases_count INTEGER DEFAULT 0,
                bankruptcies_count INTEGER DEFAULT 0,
                total_debt REAL DEFAULT 0
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_query_key ON search_results(query_key)
        """)
        self.conn.commit()

    def save(self, report: DebtReport):
        """Сохраняет отчёт в кеш."""
        key = self._make_key(report)
        data = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, default=str)

        self.conn.execute(
            """INSERT INTO search_results
               (query_key, checked_at, report_json, enforcements_count,
                court_cases_count, bankruptcies_count, total_debt)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                key,
                report.checked_at.isoformat(),
                data,
                len(report.enforcements),
                len(report.court_cases),
                len(report.bankruptcies),
                report.total_enforcement_debt,
            ),
        )
        self.conn.commit()

    def get_last(self, query_key: str) -> dict | None:
        """Получает последний результат по ключу."""
        row = self.conn.execute(
            """SELECT report_json, checked_at FROM search_results
               WHERE query_key = ? ORDER BY id DESC LIMIT 1""",
            (query_key,),
        ).fetchone()

        if row:
            return {"report": json.loads(row[0]), "checked_at": row[1]}
        return None

    def get_history(self, query_key: str, limit: int = 10) -> list[dict]:
        """Получает историю проверок."""
        rows = self.conn.execute(
            """SELECT checked_at, enforcements_count, court_cases_count,
                      bankruptcies_count, total_debt
               FROM search_results
               WHERE query_key = ? ORDER BY id DESC LIMIT ?""",
            (query_key, limit),
        ).fetchall()

        return [
            {
                "checked_at": r[0],
                "enforcements": r[1],
                "court_cases": r[2],
                "bankruptcies": r[3],
                "total_debt": r[4],
            }
            for r in rows
        ]

    def _make_key(self, report: DebtReport) -> str:
        p = report.search_params
        if p.inn:
            return f"inn:{p.inn}"
        return f"name:{p.display_name}".lower().strip()

    def close(self):
        self.conn.close()
