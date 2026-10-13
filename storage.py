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
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY, day TEXT);
            CREATE TABLE IF NOT EXISTS prefs(user_id INTEGER PRIMARY KEY, reminders INTEGER);
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

    def reminders_on(self, user_id: int) -> bool:
        row = self.db.execute("SELECT reminders FROM prefs WHERE user_id=?", (user_id,)).fetchone()
        return row is None or bool(row[0])  # за замовчуванням нагадування увімкнені

    def set_reminders(self, user_id: int, on: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO prefs VALUES(?,?)", (user_id, int(on)))
        self.db.commit()

    # --- службове: id постів, розіслані події ---
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
        self.db.commit()

    def mark_sent(self, key: str, day: str) -> bool:
        """True, якщо подію ще не розсилали (і тепер позначено). Захищає від дублів після рестарту."""
        cur = self.db.execute("INSERT OR IGNORE INTO sent VALUES(?,?)", (key, day))
        self.db.commit()
        return cur.rowcount == 1

    def cleanup(self, before_day: str) -> None:
        """Стирає старі дні: знімки, позначки подій, id постів."""
        self.db.execute("DELETE FROM sent WHERE day < ?", (before_day,))
        self.db.execute("DELETE FROM snapshots WHERE day < ?", (before_day,))
        self.db.execute("DELETE FROM meta WHERE key LIKE 'post:%' AND substr(key, 6) < ?", (before_day,))
        self.db.commit()

    # --- знімки графіків ---
    def bind_source(self, name: str) -> bool:
        """Знімки від іншого джерела (напр. demo) не можна порівнювати з новими:
        стираємо їх, щоб перемикання не розіслало хибні «зміни». True, якщо стерли."""
        row = self.db.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        if row and row[0] == name:
            return False
        self.db.execute("DELETE FROM snapshots")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('source', ?)", (name,))
        self.db.commit()
        return row is not None

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

    def get_day(self, day: str) -> dict[str, Slots]:
        rows = self.db.execute("SELECT queue, slots FROM snapshots WHERE day=?", (day,))
        return {q: [tuple(s) for s in json.loads(v)] for q, v in rows}

    def get_slots(self, day: str, queue: str) -> Slots | None:
        row = self.db.execute(
            "SELECT slots FROM snapshots WHERE day=? AND queue=?", (day, queue)
        ).fetchone()
        return [tuple(s) for s in json.loads(row[0])] if row else None
