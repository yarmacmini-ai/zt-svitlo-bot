"""SQLite: підписки на черги і знімки графіків для виявлення змін."""
import hashlib
import json
import sqlite3

Slots = list[tuple[str, str]]  # [("08:00", "12:00"), ...]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS subs(
                user_id INTEGER, queue TEXT, PRIMARY KEY(user_id, queue));
            CREATE TABLE IF NOT EXISTS snapshots(
                day TEXT, queue TEXT, hash TEXT, slots TEXT,
                PRIMARY KEY(day, queue));
            """
        )

    # --- підписки ---
    def toggle(self, user_id: int, queue: str) -> bool:
        """Вмикає/вимикає підписку. Повертає True, якщо тепер підписаний."""
        cur = self.db.execute(
            "DELETE FROM subs WHERE user_id=? AND queue=?", (user_id, queue))
        if cur.rowcount == 0:
            self.db.execute("INSERT INTO subs VALUES(?,?)", (user_id, queue))
            self.db.commit()
            return True
        self.db.commit()
        return False

    def unsubscribe_all(self, user_id: int) -> None:
        self.db.execute("DELETE FROM subs WHERE user_id=?", (user_id,))
        self.db.commit()

    def user_queues(self, user_id: int) -> list[str]:
        rows = self.db.execute(
            "SELECT queue FROM subs WHERE user_id=? ORDER BY queue", (user_id,))
        return [r[0] for r in rows]

    def subscribers(self, queue: str) -> list[int]:
        rows = self.db.execute("SELECT user_id FROM subs WHERE queue=?", (queue,))
        return [r[0] for r in rows]

    def subscriber_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM subs").fetchone()[0]

    # --- знімки графіків ---
    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is None

    def update_snapshot(self, day: str, queue: str, slots: Slots) -> bool:
        """Зберігає графік. True, якщо він новий або змінився."""
        payload = json.dumps(sorted(map(list, slots)))
        digest = hashlib.sha1(payload.encode()).hexdigest()
        row = self.db.execute(
            "SELECT hash FROM snapshots WHERE day=? AND queue=?", (day, queue)
        ).fetchone()
        if row and row[0] == digest:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)",
            (day, queue, digest, payload))
        self.db.commit()
        return True

    def get_slots(self, day: str, queue: str) -> Slots | None:
        row = self.db.execute(
            "SELECT slots FROM snapshots WHERE day=? AND queue=?", (day, queue)
        ).fetchone()
        return [tuple(s) for s in json.loads(row[0])] if row else None
