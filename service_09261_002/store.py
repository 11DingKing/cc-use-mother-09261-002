"""SQLite 状态仓储：持久化冻结快照。"""
import json
import sqlite3


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots("
            "unit_id TEXT PRIMARY KEY, body TEXT NOT NULL,"
            "saved_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        self.db.commit()

    def save_snapshot(self, unit_id, body):
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
        self.db.execute("INSERT OR REPLACE INTO snapshots(unit_id, body) VALUES(?, ?)", (unit_id, payload))
        self.db.commit()

    def get_snapshot(self, unit_id):
        row = self.db.execute("SELECT body FROM snapshots WHERE unit_id=?", (unit_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_snapshots(self):
        rows = self.db.execute("SELECT body FROM snapshots ORDER BY unit_id").fetchall()
        return [json.loads(r[0]) for r in rows]
