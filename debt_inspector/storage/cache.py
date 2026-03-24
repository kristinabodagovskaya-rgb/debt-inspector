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
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                claimant TEXT DEFAULT 'ФНС России',
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_manual_debts_key ON manual_debts(query_key)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bankruptcy_pipelines (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                current_step TEXT NOT NULL DEFAULT 'assessment',
                data_json TEXT NOT NULL DEFAULT '{}'
            )
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

    def save_manual_debt(self, query_key: str, description: str, amount: float, claimant: str = "ФНС России"):
        """Сохраняет ручной долг в БД."""
        self.conn.execute(
            """INSERT INTO manual_debts (query_key, description, amount, claimant, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (query_key, description, amount, claimant, datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_manual_debts(self, query_key: str) -> list[dict]:
        """Получает все ручные долги для данного ключа."""
        rows = self.conn.execute(
            """SELECT description, amount, claimant FROM manual_debts
               WHERE query_key = ? ORDER BY id""",
            (query_key,),
        ).fetchall()
        return [{"description": r[0], "amount": r[1], "claimant": r[2]} for r in rows]

    def delete_manual_debt(self, debt_id: int):
        """Удаляет ручной долг."""
        self.conn.execute("DELETE FROM manual_debts WHERE id = ?", (debt_id,))
        self.conn.commit()

    def make_key(self, params) -> str:
        """Создаёт ключ из SearchParams или DebtReport."""
        if hasattr(params, "search_params"):
            p = params.search_params
        else:
            p = params
        if p.inn:
            return f"inn:{p.inn}"
        return f"name:{p.display_name}".lower().strip()

    def _make_key(self, report: DebtReport) -> str:
        return self.make_key(report)

    # --- Pipeline банкротства ---

    def save_pipeline(self, pipeline_id: str, step: str, data: dict):
        """Сохраняет или обновляет pipeline."""
        now = datetime.now().isoformat()
        data_str = json.dumps(data, ensure_ascii=False, default=str)
        existing = self.conn.execute(
            "SELECT id FROM bankruptcy_pipelines WHERE id = ?", (pipeline_id,)
        ).fetchone()
        if existing:
            self.conn.execute(
                """UPDATE bankruptcy_pipelines
                   SET updated_at = ?, current_step = ?, data_json = ?
                   WHERE id = ?""",
                (now, step, data_str, pipeline_id),
            )
        else:
            self.conn.execute(
                """INSERT INTO bankruptcy_pipelines (id, created_at, updated_at, current_step, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (pipeline_id, now, now, step, data_str),
            )
        self.conn.commit()

    def get_pipeline(self, pipeline_id: str) -> dict | None:
        """Получает pipeline по ID."""
        row = self.conn.execute(
            "SELECT current_step, data_json, created_at, updated_at FROM bankruptcy_pipelines WHERE id = ?",
            (pipeline_id,),
        ).fetchone()
        if row:
            return {
                "current_step": row[0],
                "data": json.loads(row[1]),
                "created_at": row[2],
                "updated_at": row[3],
            }
        return None

    def delete_pipeline(self, pipeline_id: str):
        """Удаляет pipeline."""
        self.conn.execute("DELETE FROM bankruptcy_pipelines WHERE id = ?", (pipeline_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()
