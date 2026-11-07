"""Historial local de consultas verificadas, separado por clave de cuenta."""

from contextlib import closing
from sqlite3 import connect


class ConsultationHistory:
    def __init__(self, path):
        self.path = path

    def record(self, account, result):
        with closing(connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS consultation (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, timestamp TEXT NOT NULL,
                    establishment TEXT NOT NULL, service TEXT NOT NULL, specialty TEXT NOT NULL,
                    slots INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS consultation_account ON consultation(account, id)")
            db.execute(
                """INSERT INTO consultation(account, timestamp, establishment, service, specialty, slots)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account, result.timestamp.isoformat(timespec="seconds"), result.establishment,
                 result.service, result.specialty, len(result.rows)),
            )
            db.execute(
                """DELETE FROM consultation WHERE account = ? AND id IN (
                       SELECT id FROM consultation WHERE account = ? ORDER BY id DESC LIMIT -1 OFFSET 100
                   )""",
                (account, account),
            )

    def recent(self, account):
        if not self.path.is_file():
            return []
        with closing(connect(self.path)) as db:
            return db.execute(
                """SELECT timestamp, establishment, service, specialty, slots
                   FROM consultation WHERE account = ? ORDER BY id DESC""",
                (account,),
            ).fetchall()
