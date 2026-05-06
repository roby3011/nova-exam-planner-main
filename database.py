"""SQLite storage for users, courses, constraints, and study sessions."""

import os
import sqlite3
import datetime as dt
from contextlib import contextmanager
from typing import Optional

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "nova.db")
DEFAULT_COUNTRY = "PT"


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


@contextmanager
def get_connection():
    """Open SQLite with foreign keys and dict-like rows."""
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables and indexes if needed."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                email         TEXT,
                password_hash TEXT NOT NULL,
                display_name  TEXT,
                country_code  TEXT DEFAULT 'PT',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS courses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                name            TEXT NOT NULL,
                exam_date       DATE NOT NULL,
                ects            REAL NOT NULL,
                difficulty      INTEGER NOT NULL,
                estimated_hours REAL NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS constraints (
                user_id           INTEGER PRIMARY KEY,
                weekly_hours      INTEGER DEFAULT 30,
                preferred_days    TEXT    DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday',
                max_hours_per_day INTEGER DEFAULT 8,
                start_date        DATE,
                skip_holidays     INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS study_sessions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL,
                course_id         INTEGER NOT NULL,
                session_date      DATE NOT NULL,
                planned_minutes   INTEGER NOT NULL,
                completed_minutes INTEGER DEFAULT 0,
                FOREIGN KEY (user_id)   REFERENCES users(id)   ON DELETE CASCADE,
                FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_courses_user    ON courses(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user   ON study_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_course ON study_sessions(course_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_date   ON study_sessions(session_date);
        """)


def create_user(username: str, email: Optional[str], password_hash: str,
                display_name: Optional[str] = None,
                country_code: str = DEFAULT_COUNTRY) -> int:
    """Create a user with default constraints."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, email, password_hash, display_name, country_code)
               VALUES (?, ?, ?, ?, ?)""",
            (username, email, password_hash, display_name or username, country_code),
        )
        user_id = cur.lastrowid
        conn.execute(
            "INSERT INTO constraints (user_id, start_date) VALUES (?, ?)",
            (user_id, dt.date.today().isoformat()),
        )
        return user_id


def get_user_by_username(username: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def update_user_profile(user_id: int, *, display_name: Optional[str] = None,
                        email: Optional[str] = None,
                        country_code: Optional[str] = None):
    sets, params = [], []
    for col, val in (("display_name", display_name),
                     ("email", email),
                     ("country_code", country_code)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    params.append(user_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)


def update_user_password(user_id: int, new_password_hash: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_password_hash, user_id),
        )


def list_courses(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM courses WHERE user_id = ?
               ORDER BY exam_date, name""",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["exam_date"] = dt.date.fromisoformat(d["exam_date"])
        out.append(d)
    return out


def get_course(user_id: int, course_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM courses WHERE id = ? AND user_id = ?",
            (course_id, user_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["exam_date"] = dt.date.fromisoformat(d["exam_date"])
        return d


def upsert_course(user_id: int, name: str, exam_date: dt.date,
                  ects: float, difficulty: int, estimated_hours: float,
                  course_id: Optional[int] = None) -> int:
    """Save a course and return its id."""
    with get_connection() as conn:
        if course_id:
            conn.execute(
                """UPDATE courses
                   SET name=?, exam_date=?, ects=?, difficulty=?, estimated_hours=?
                   WHERE id=? AND user_id=?""",
                (name, exam_date.isoformat(), ects, difficulty,
                 estimated_hours, course_id, user_id),
            )
            return course_id
        cur = conn.execute(
            """INSERT INTO courses
               (user_id, name, exam_date, ects, difficulty, estimated_hours)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, name, exam_date.isoformat(), ects, difficulty,
             estimated_hours),
        )
        return cur.lastrowid


def delete_course(user_id: int, course_id: int):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM courses WHERE id = ? AND user_id = ?",
            (course_id, user_id),
        )


def get_constraints(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM constraints WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return {
            "user_id": user_id,
            "weekly_hours": 30,
            "preferred_days": DAY_NAMES_DEFAULT,
            "max_hours_per_day": 8,
            "start_date": dt.date.today(),
            "skip_holidays": True,
        }
    d = dict(row)
    d["preferred_days"] = [x for x in d["preferred_days"].split(",") if x]
    d["start_date"] = dt.date.fromisoformat(d["start_date"]) \
        if d["start_date"] else dt.date.today()
    d["skip_holidays"] = bool(d["skip_holidays"])
    return d


def save_constraints(user_id: int, *, weekly_hours: int,
                     preferred_days: list[str], max_hours_per_day: int,
                     start_date: dt.date, skip_holidays: bool):
    days_str = ",".join(preferred_days)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO constraints (user_id, weekly_hours, preferred_days,
                                     max_hours_per_day, start_date, skip_holidays)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                weekly_hours      = excluded.weekly_hours,
                preferred_days    = excluded.preferred_days,
                max_hours_per_day = excluded.max_hours_per_day,
                start_date        = excluded.start_date,
                skip_holidays     = excluded.skip_holidays
        """, (user_id, weekly_hours, days_str, max_hours_per_day,
              start_date.isoformat(), int(skip_holidays)))


def list_sessions(user_id: int) -> list[dict]:
    """Return sessions with their course name."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT s.id, s.course_id, s.session_date, s.planned_minutes,
                   s.completed_minutes, c.name AS course_name
            FROM study_sessions s
            JOIN courses c ON c.id = s.course_id
            WHERE s.user_id = ?
            ORDER BY s.session_date, c.name
        """, (user_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["session_date"] = dt.date.fromisoformat(d["session_date"])
        out.append(d)
    return out


def replace_sessions(user_id: int, sessions: list[dict]):
    """Replace the generated plan."""
    with get_connection() as conn:
        conn.execute("DELETE FROM study_sessions WHERE user_id = ?", (user_id,))
        payload = []
        for s in sessions:
            d = s["session_date"]
            if isinstance(d, dt.date):
                d = d.isoformat()
            payload.append((
                user_id, s["course_id"], d,
                int(s["planned_minutes"]),
                int(s.get("completed_minutes", 0)),
            ))
        conn.executemany("""
            INSERT INTO study_sessions
                (user_id, course_id, session_date, planned_minutes, completed_minutes)
            VALUES (?, ?, ?, ?, ?)
        """, payload)


def update_session_planned(user_id: int, session_id: int, planned_minutes: int):
    with get_connection() as conn:
        conn.execute(
            """UPDATE study_sessions SET planned_minutes = ?
               WHERE id = ? AND user_id = ?""",
            (max(0, int(planned_minutes)), session_id, user_id),
        )


def update_session_completed(user_id: int, session_id: int, completed_minutes: int):
    with get_connection() as conn:
        conn.execute(
            """UPDATE study_sessions SET completed_minutes = ?
               WHERE id = ? AND user_id = ?""",
            (max(0, int(completed_minutes)), session_id, user_id),
        )


def insert_session(user_id: int, course_id: int, session_date: dt.date,
                   planned_minutes: int, completed_minutes: int = 0) -> int:
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO study_sessions
                (user_id, course_id, session_date, planned_minutes, completed_minutes)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            course_id,
            session_date.isoformat(),
            max(0, int(planned_minutes)),
            max(0, int(completed_minutes)),
        ))
        return cur.lastrowid


def delete_session(user_id: int, session_id: int):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM study_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )


def has_plan(user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM study_sessions WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None


def clear_user_data(user_id: int):
    """Clear courses and sessions for one user."""
    with get_connection() as conn:
        conn.execute("DELETE FROM study_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM courses WHERE user_id = ?", (user_id,))


DAY_NAMES_DEFAULT = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]
