"""SQLite 状态仓储：状态快照日志、事件日志、冻结快照。"""
import json
import sqlite3


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots"
                        "(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events"
                        "(seq INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT NOT NULL,body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS frozen"
                        "(case_id TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,"
                        "PRIMARY KEY(case_id,version))")
        self.db.commit()

    def save(self, value):
        self.db.execute("INSERT INTO snapshots(body) VALUES(?)",
                        (json.dumps(value, ensure_ascii=False),))
        self.db.commit()

    def latest(self):
        row = self.db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def save_event(self, event):
        self.db.execute("INSERT INTO events(case_id,body) VALUES(?,?)",
                        (event["case_id"], json.dumps(event, ensure_ascii=False)))
        self.db.commit()

    def events(self, case_id=None):
        if case_id is None:
            rows = self.db.execute("SELECT body FROM events ORDER BY seq").fetchall()
        else:
            rows = self.db.execute(
                "SELECT body FROM events WHERE case_id=? ORDER BY seq", (case_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_frozen(self, snapshot):
        self.db.execute("INSERT OR IGNORE INTO frozen(case_id,version,body) VALUES(?,?,?)",
                        (snapshot["case_id"], snapshot["version"],
                         json.dumps(snapshot, ensure_ascii=False)))
        self.db.commit()

    def frozen(self, case_id=None):
        if case_id is None:
            rows = self.db.execute("SELECT body FROM frozen ORDER BY case_id,version").fetchall()
        else:
            rows = self.db.execute(
                "SELECT body FROM frozen WHERE case_id=? ORDER BY version", (case_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]
