"""
J.A.R.V.I.S. v5.3 — Database
Stage 1: архив, хронология, веб-хуки (Zapier/Make), ключи задач (JV-1), настройки.
"""

import sqlite3
import os
import secrets
import json
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "jarvis.db"))


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            emoji TEXT DEFAULT '📁',
            created_at TEXT NOT NULL
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_name TEXT DEFAULT '',
            project_id INTEGER DEFAULT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            category TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            deadline TEXT DEFAULT NULL,
            status TEXT DEFAULT 'active',
            recurrence TEXT DEFAULT NULL,
            recurrence_end TEXT DEFAULT NULL,
            reminded_15min INTEGER DEFAULT 0,
            reminded_overdue INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT DEFAULT NULL,
            kanban_column TEXT DEFAULT 'todo',
            kanban_order INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS task_assignees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            assigned_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT DEFAULT '',
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS task_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT DEFAULT 'document',
            file_name TEXT DEFAULT '',
            added_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS user_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            tasks_created INTEGER DEFAULT 0,
            tasks_completed INTEGER DEFAULT 0,
            tasks_assigned INTEGER DEFAULT 0
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )""")

        # Таблица для отслеживания пользователей (username → user_id)
        conn.execute("""CREATE TABLE IF NOT EXISTS known_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            chat_id INTEGER DEFAULT 0,
            last_seen TEXT NOT NULL
        )""")

        # Таблица разрешённых пользователей (приватный бот)
        conn.execute("""CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            added_by INTEGER DEFAULT 0,
            added_at TEXT NOT NULL
        )""")

        # Таблица разрешённых чатов/групп
        conn.execute("""CREATE TABLE IF NOT EXISTS allowed_chats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT DEFAULT '',
            added_by INTEGER DEFAULT 0,
            added_at TEXT NOT NULL
        )""")

        # Настройки чата (ключ задач, авто-архив и т.д.)
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            key_prefix TEXT DEFAULT 'JV',
            auto_archive_days INTEGER DEFAULT 7,
            timezone_offset INTEGER DEFAULT 5,
            settings_json TEXT DEFAULT '{}'
        )""")

        # Веб-хуки для Zapier / Make.com / любых внешних сервисов
        conn.execute("""CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            events TEXT NOT NULL DEFAULT 'task.created,task.completed',
            is_active INTEGER DEFAULT 1,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            last_triggered_at TEXT DEFAULT NULL,
            trigger_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT ''
        )""")

        # Миграции колонок задач — добавляем безопасно, не падая если уже есть
        _migrate_column(conn, "tasks", "archived_at", "TEXT DEFAULT NULL")
        _migrate_column(conn, "tasks", "start_date", "TEXT DEFAULT NULL")
        _migrate_column(conn, "tasks", "task_type", "TEXT DEFAULT 'task'")
        _migrate_column(conn, "tasks", "parent_id", "INTEGER DEFAULT NULL")


def _migrate_column(conn, table, column, definition):
    """Безопасно добавляет колонку в таблицу. Игнорирует ошибку если колонка уже есть."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass


# ═══════════════════════════════════════════════════════════════
#  ОТСЛЕЖИВАНИЕ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════

def track_user(user_id, username="", first_name="", chat_id=0):
    """Запоминает пользователя когда он пишет в чат."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM known_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE known_users SET username=?, first_name=?, chat_id=?, last_seen=? WHERE user_id=?",
                (username, first_name, chat_id, now, user_id))
        else:
            conn.execute(
                "INSERT INTO known_users (user_id, username, first_name, chat_id, last_seen) VALUES (?,?,?,?,?)",
                (user_id, username, first_name, chat_id, now))

def get_user_id_by_username(username):
    """Находит user_id по username."""
    username = username.lstrip("@").lower()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM known_users WHERE LOWER(username)=?", (username,)).fetchone()
        return row["user_id"] if row else None

def get_all_notifiable_users_for_task(task_id):
    """Возвращает список user_id для уведомлений: создатель + все исполнители."""
    with get_connection() as conn:
        task = conn.execute("SELECT creator_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return []
        user_ids = set()
        user_ids.add(task["creator_id"])
        assignees = conn.execute(
            "SELECT user_id, username FROM task_assignees WHERE task_id=?", (task_id,)).fetchall()
        for a in assignees:
            if a["user_id"] and a["user_id"] != 0:
                user_ids.add(a["user_id"])
            elif a["username"]:
                uid = get_user_id_by_username(a["username"])
                if uid:
                    user_ids.add(uid)
        return list(user_ids)


# ═══════════════════════════════════════════════════════════════
#  КОНТРОЛЬ ДОСТУПА (ПРИВАТНЫЙ БОТ)
# ═══════════════════════════════════════════════════════════════

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    admin_ids_str = os.environ.get("ADMIN_IDS", "")
    if not admin_ids_str:
        return False
    admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
    return user_id in admin_ids

def add_allowed_user(user_id, username="", first_name="", added_by=0):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO allowed_users (user_id, username, first_name, added_by, added_at)
               VALUES (?,?,?,?,?)""",
            (user_id, username, first_name, added_by, now))

def remove_allowed_user(user_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))

def is_user_allowed(user_id, username=""):
    """Проверяет, есть ли пользователь в списке разрешённых (или он админ)."""
    if is_admin(user_id):
        return True
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM allowed_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return True
        # Проверяем по username (если добавлен по @username без user_id)
        if username:
            row = conn.execute(
                "SELECT user_id FROM allowed_users WHERE LOWER(username)=LOWER(?)",
                (username,)).fetchone()
            if row:
                # Обновляем user_id в записи
                conn.execute("UPDATE allowed_users SET user_id=? WHERE LOWER(username)=LOWER(?)",
                             (user_id, username))
                return True
    return False

def get_allowed_users():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM allowed_users ORDER BY added_at DESC").fetchall()]

def add_allowed_chat(chat_id, chat_title="", added_by=0):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO allowed_chats (chat_id, chat_title, added_by, added_at)
               VALUES (?,?,?,?)""",
            (chat_id, chat_title, added_by, now))

def remove_allowed_chat(chat_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM allowed_chats WHERE chat_id=?", (chat_id,))

def is_chat_allowed(chat_id):
    """Проверяет, разрешён ли чат/группа."""
    with get_connection() as conn:
        row = conn.execute("SELECT chat_id FROM allowed_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row is not None

def get_allowed_chats():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM allowed_chats ORDER BY added_at DESC").fetchall()]

def check_access(user_id, chat_id, username=""):
    """Главная проверка: пользователь имеет доступ?
    Доступ есть если: админ, или в списке allowed_users, или чат в allowed_chats."""
    if is_admin(user_id):
        return True
    if is_user_allowed(user_id, username):
        return True
    if is_chat_allowed(chat_id):
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  ПРОЕКТЫ
# ═══════════════════════════════════════════════════════════════

def create_project(chat_id, name, description="", emoji="📁"):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (chat_id, name, description, emoji, created_at) VALUES (?,?,?,?,?)",
            (chat_id, name, description, emoji, now))
        return get_project(cur.lastrowid)

def get_project(project_id):
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(r) if r else None

def get_projects(chat_id):
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM projects WHERE chat_id=? ORDER BY name", (chat_id,)).fetchall()]

def delete_project(project_id):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET project_id=NULL WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


# ═══════════════════════════════════════════════════════════════
#  ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════

def add_task(chat_id, creator_id, title, description="", priority="medium",
             category="", deadline=None, creator_name="", recurrence=None,
             recurrence_end=None, tags="", project_id=None):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (chat_id, creator_id, creator_name, project_id, title,
               description, priority, category, tags, deadline, created_at,
               recurrence, recurrence_end, kanban_column, kanban_order)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'todo',0)""",
            (chat_id, creator_id, creator_name, project_id, title, description,
             priority, category, tags, deadline, now, recurrence, recurrence_end))
        _update_stat(conn, creator_id, chat_id, creator_name, "", "tasks_created")
    return get_task(cur.lastrowid)

def get_task(task_id):
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r:
            return None
        t = dict(r)
        t["assignees"] = _get_assignees(conn, task_id)
        t["files"] = _get_files(conn, task_id)
        t["subtasks"] = _get_subtasks(conn, task_id)
        t["comment_count"] = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE task_id=?", (task_id,)).fetchone()[0]
        return t

def get_active_tasks(chat_id, project_id=None):
    with get_connection() as conn:
        if project_id:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE chat_id=? AND status='active' AND project_id=?
                   AND archived_at IS NULL
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                   CASE WHEN deadline IS NOT NULL THEN 0 ELSE 1 END, deadline ASC""",
                (chat_id, project_id)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE chat_id=? AND status='active'
                   AND archived_at IS NULL
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                   CASE WHEN deadline IS NOT NULL THEN 0 ELSE 1 END, deadline ASC""",
                (chat_id,)).fetchall()
        return [_enrich(conn, r) for r in rows]

def get_all_tasks(chat_id):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks WHERE chat_id=? AND archived_at IS NULL
               ORDER BY status ASC, created_at DESC""",
            (chat_id,)).fetchall()
        return [_enrich(conn, r) for r in rows]

def get_user_tasks(chat_id, user_id):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.* FROM tasks t
               LEFT JOIN task_assignees a ON t.id = a.task_id
               WHERE t.chat_id=? AND t.status='active'
               AND (t.creator_id=? OR a.user_id=?)
               ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END""",
            (chat_id, user_id, user_id)).fetchall()
        return [_enrich(conn, r) for r in rows]

def search_tasks(chat_id, query):
    q = f"%{query}%"
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks WHERE chat_id=?
               AND (title LIKE ? OR description LIKE ? OR tags LIKE ? OR category LIKE ?)
               ORDER BY status ASC, created_at DESC LIMIT 20""",
            (chat_id, q, q, q, q)).fetchall()
        return [_enrich(conn, r) for r in rows]

def complete_task(task_id, user_id, chat_id=None):
    now = datetime.now().isoformat()
    task = get_task(task_id)
    if not task:
        return None
    if chat_id and task["chat_id"] != chat_id:
        return None
    if not chat_id and task["creator_id"] != user_id:
        return None
    with get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, kanban_column='done' WHERE id=?",
            (now, task_id))
        _update_stat(conn, user_id, task["chat_id"], "", "", "tasks_completed")
    if task.get("recurrence"):
        _create_next_recurring(task)
    return get_task(task_id)

def delete_task(task_id, user_id, chat_id=None):
    task = get_task(task_id)
    if not task:
        return False
    if chat_id and task["chat_id"] != chat_id:
        return False
    if not chat_id and task["creator_id"] != user_id:
        return False
    with get_connection() as conn:
        for tbl in ["subtasks", "comments", "task_assignees", "task_files"]:
            conn.execute(f"DELETE FROM {tbl} WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return True

def clear_done_tasks(chat_id):
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM tasks WHERE chat_id=? AND status='done'", (chat_id,)).rowcount

def update_priority(task_id, user_id, priority):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (priority, task_id))
    return get_task(task_id)

def update_tags(task_id, tags):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET tags=? WHERE id=?", (tags, task_id))
    return get_task(task_id)

def set_project(task_id, project_id):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET project_id=? WHERE id=?", (project_id, task_id))
    return get_task(task_id)

def postpone_task(task_id, user_id, delta):
    task = get_task(task_id)
    if not task:
        return None
    if task.get("deadline"):
        old = datetime.fromisoformat(task["deadline"])
        new_dl = (datetime.now() if old < datetime.now() else old) + delta
    else:
        new_dl = datetime.now() + delta
    with get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET deadline=?, reminded_15min=0, reminded_overdue=0 WHERE id=?",
            (new_dl.isoformat(), task_id))
    return get_task(task_id)


# ═══════════════════════════════════════════════════════════════
#  НАЗНАЧЕНИЕ ИСПОЛНИТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════

def add_assignee(task_id, user_id, username="", first_name=""):
    now = datetime.now().isoformat()
    # Если user_id=0 но есть username, пробуем найти реальный user_id
    if (not user_id or user_id == 0) and username:
        real_id = get_user_id_by_username(username)
        if real_id:
            user_id = real_id
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT id FROM task_assignees WHERE task_id=? AND (user_id=? OR LOWER(username)=LOWER(?))",
            (task_id, user_id, username)).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO task_assignees (task_id, user_id, username, first_name, assigned_at) VALUES (?,?,?,?,?)",
            (task_id, user_id, username, first_name, now))
        task = conn.execute("SELECT chat_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task:
            _update_stat(conn, user_id, task["chat_id"], first_name, username, "tasks_assigned")
    return True

def _get_assignees(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM task_assignees WHERE task_id=?", (task_id,)).fetchall()]


# ═══════════════════════════════════════════════════════════════
#  ПОДЗАДАЧИ
# ═══════════════════════════════════════════════════════════════

def add_subtask(task_id, title):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO subtasks (task_id, title, created_at) VALUES (?,?,?)",
            (task_id, title, now))

def toggle_subtask(subtask_id):
    with get_connection() as conn:
        r = conn.execute("SELECT done FROM subtasks WHERE id=?", (subtask_id,)).fetchone()
        if r:
            new = 0 if r["done"] else 1
            conn.execute("UPDATE subtasks SET done=? WHERE id=?", (new, subtask_id))
            return new
    return None

def _get_subtasks(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM subtasks WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]


# ═══════════════════════════════════════════════════════════════
#  КОММЕНТАРИИ
# ═══════════════════════════════════════════════════════════════

def add_comment(task_id, user_id, user_name, text):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO comments (task_id, user_id, user_name, text, created_at) VALUES (?,?,?,?,?)",
            (task_id, user_id, user_name, text, now))

def get_comments(task_id, limit=20):
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM comments WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit)).fetchall()]


# ═══════════════════════════════════════════════════════════════
#  ФАЙЛЫ
# ═══════════════════════════════════════════════════════════════

def add_file(task_id, file_id, file_type="document", file_name=""):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO task_files (task_id, file_id, file_type, file_name, added_at) VALUES (?,?,?,?,?)",
            (task_id, file_id, file_type, file_name, now))

def _get_files(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM task_files WHERE task_id=?", (task_id,)).fetchall()]


# ═══════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════

def _update_stat(conn, user_id, chat_id, user_name="", username="", field="tasks_created"):
    row = conn.execute(
        "SELECT id FROM user_stats WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)).fetchone()
    if row:
        conn.execute(f"UPDATE user_stats SET {field}={field}+1 WHERE id=?", (row["id"],))
        if user_name:
            conn.execute("UPDATE user_stats SET user_name=? WHERE id=?", (user_name, row["id"]))
    else:
        conn.execute(
            f"INSERT INTO user_stats (user_id, chat_id, user_name, username, {field}) VALUES (?,?,?,?,1)",
            (user_id, chat_id, user_name, username))

def get_stats(chat_id):
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM user_stats WHERE chat_id=?
               ORDER BY tasks_completed DESC, tasks_created DESC""",
            (chat_id,)).fetchall()]


# ═══════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ
# ═══════════════════════════════════════════════════════════════

def get_tasks_needing_reminder():
    now_iso = datetime.now().isoformat()
    soon = (datetime.now() + timedelta(minutes=15)).isoformat()
    with get_connection() as conn:
        upcoming = [_enrich(conn, r) for r in conn.execute(
            """SELECT * FROM tasks WHERE status='active'
               AND deadline IS NOT NULL AND deadline<=? AND deadline>? AND reminded_15min=0""",
            (soon, now_iso)).fetchall()]
        overdue = [_enrich(conn, r) for r in conn.execute(
            """SELECT * FROM tasks WHERE status='active'
               AND deadline IS NOT NULL AND deadline<? AND reminded_overdue=0""",
            (now_iso,)).fetchall()]
        return {"upcoming": upcoming, "overdue": overdue}

def mark_reminded(task_id, reminder_type):
    col = "reminded_15min" if reminder_type == "15min" else "reminded_overdue"
    with get_connection() as conn:
        conn.execute(f"UPDATE tasks SET {col}=1 WHERE id=?", (task_id,))

def get_all_chats_with_tasks():
    with get_connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT chat_id FROM tasks WHERE status='active'").fetchall()]


# ═══════════════════════════════════════════════════════════════
#  ПОВТОРЯЮЩИЕСЯ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════

def _create_next_recurring(task):
    rec = task.get("recurrence")
    if not rec or not task.get("deadline"):
        return
    old_dl = datetime.fromisoformat(task["deadline"])
    if rec == "daily":
        new_dl = old_dl + timedelta(days=1)
    elif rec == "weekly":
        new_dl = old_dl + timedelta(weeks=1)
    elif rec == "monthly":
        m, y = old_dl.month + 1, old_dl.year
        if m > 12:
            m, y = 1, y + 1
        new_dl = old_dl.replace(year=y, month=m)
    else:
        return
    if task.get("recurrence_end") and new_dl > datetime.fromisoformat(task["recurrence_end"]):
        return
    new = add_task(
        task["chat_id"], task["creator_id"], task["title"],
        task.get("description", ""), task["priority"], task.get("category", ""),
        new_dl.isoformat(), task.get("creator_name", ""),
        rec, task.get("recurrence_end"), task.get("tags", ""), task.get("project_id"))
    if new and task.get("assignees"):
        for a in task["assignees"]:
            add_assignee(new["id"], a["user_id"], a.get("username", ""), a.get("first_name", ""))


def _enrich(conn, row):
    t = dict(row)
    t["assignees"] = _get_assignees(conn, t["id"])
    t["files"] = _get_files(conn, t["id"])
    t["subtasks"] = _get_subtasks(conn, t["id"])
    t["comment_count"] = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE task_id=?", (t["id"],)).fetchone()[0]
    return t


# ═══════════════════════════════════════════════════════════════
#  ДАШБОРД — ТОКЕНЫ
# ═══════════════════════════════════════════════════════════════

def generate_dashboard_token(user_id, chat_id, username="", hours=876000):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(hours=hours)
    with get_connection() as conn:
        conn.execute(
            "UPDATE dashboard_tokens SET is_active=0 WHERE user_id=? AND chat_id=?",
            (user_id, chat_id))
        conn.execute(
            """INSERT INTO dashboard_tokens (token, user_id, chat_id, username, created_at, expires_at)
               VALUES (?,?,?,?,?,?)""",
            (token, user_id, chat_id, username, now.isoformat(), expires.isoformat()))
    return token

def validate_token(token):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM dashboard_tokens WHERE token=? AND is_active=1 AND expires_at>?",
            (token, datetime.now().isoformat())).fetchone()
        return dict(row) if row else None

def revoke_token(token):
    with get_connection() as conn:
        conn.execute("UPDATE dashboard_tokens SET is_active=0 WHERE token=?", (token,))


# ═══════════════════════════════════════════════════════════════
#  ДАШБОРД — CRUD
# ═══════════════════════════════════════════════════════════════

def get_tasks_for_dashboard(user_id, chat_id=None, status=None, include_archived=False):
    with get_connection() as conn:
        query = "SELECT * FROM tasks WHERE (creator_id=? OR id IN (SELECT task_id FROM task_assignees WHERE user_id=?))"
        params = [user_id, user_id]
        if not include_archived:
            query += " AND archived_at IS NULL"
        if chat_id and chat_id != 0:
            query += " AND (chat_id=? OR chat_id=0)"
            params.append(chat_id)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY kanban_order ASC, id DESC"
        rows = conn.execute(query, params).fetchall()
        return [_enrich(conn, r) for r in rows]

def create_task_from_dashboard(user_id, chat_id, title, description="",
                                priority="medium", category="", deadline=None,
                                kanban_column="todo"):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(kanban_order),0) FROM tasks WHERE creator_id=? AND kanban_column=?",
            (user_id, kanban_column)).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO tasks (chat_id, creator_id, title, description, priority,
               category, deadline, status, kanban_column, kanban_order, created_at)
               VALUES (?,?,?,?,?,?,?,'active',?,?,?)""",
            (chat_id, user_id, title, description, priority,
             category, deadline, kanban_column, max_order + 1, now))
        return cur.lastrowid

def update_task_from_dashboard(task_id, user_id, **fields):
    allowed = {"title", "description", "priority", "category", "deadline",
               "status", "kanban_column", "kanban_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if updates.get("kanban_column") == "done":
        updates["status"] = "done"
        updates["completed_at"] = datetime.now().isoformat()
    elif updates.get("kanban_column") in ("todo", "in_progress"):
        updates["status"] = "active"
        updates["completed_at"] = None
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [task_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)
    return True

def delete_task_from_dashboard(task_id, user_id):
    with get_connection() as conn:
        for tbl in ["subtasks", "comments", "task_assignees", "task_files"]:
            conn.execute(f"DELETE FROM {tbl} WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return True

def reorder_kanban(user_id, task_id, new_column, new_order):
    with get_connection() as conn:
        conn.execute(
            """UPDATE tasks SET kanban_column=?, kanban_order=?,
               status=CASE WHEN ?='done' THEN 'done' ELSE 'active' END,
               completed_at=CASE WHEN ?='done' THEN ? ELSE NULL END
               WHERE id=?""",
            (new_column, new_order, new_column, new_column,
             datetime.now().isoformat(), task_id))
    return True

def get_dashboard_stats(user_id, chat_id=None):
    with get_connection() as conn:
        base_where = "WHERE (creator_id=? OR id IN (SELECT task_id FROM task_assignees WHERE user_id=?))"
        params = [user_id, user_id]
        if chat_id and chat_id != 0:
            base_where += " AND (chat_id=? OR chat_id=0)"
            params.append(chat_id)
        total = conn.execute(f"SELECT COUNT(*) FROM tasks {base_where}", params).fetchone()[0]
        active = conn.execute(
            f"SELECT COUNT(*) FROM tasks {base_where} AND status='active'", params).fetchone()[0]
        done = conn.execute(
            f"SELECT COUNT(*) FROM tasks {base_where} AND status='done'", params).fetchone()[0]
        now = datetime.now().isoformat()
        overdue = conn.execute(
            f"SELECT COUNT(*) FROM tasks {base_where} AND status='active' AND deadline IS NOT NULL AND deadline<?",
            params + [now]).fetchone()[0]
        high = conn.execute(
            f"SELECT COUNT(*) FROM tasks {base_where} AND status='active' AND priority='high'",
            params).fetchone()[0]
        return {
            "total": total, "active": active, "done": done,
            "overdue": overdue, "high_priority": high,
            "completion_rate": round(done / total * 100, 1) if total > 0 else 0
        }


# ═══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ ЧАТА
# ═══════════════════════════════════════════════════════════════

def get_chat_settings(chat_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            d = dict(row)
            try:
                d["settings_json"] = json.loads(d.get("settings_json") or "{}")
            except Exception:
                d["settings_json"] = {}
            return d
        # Создаём дефолтные настройки
        conn.execute(
            "INSERT INTO chat_settings (chat_id, key_prefix, auto_archive_days, timezone_offset) VALUES (?,?,?,?)",
            (chat_id, "JV", 7, 5))
        return {"chat_id": chat_id, "key_prefix": "JV", "auto_archive_days": 7,
                "timezone_offset": 5, "settings_json": {}}


def update_chat_settings(chat_id, **fields):
    allowed = {"key_prefix", "auto_archive_days", "timezone_offset"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    # Убедимся что запись существует
    get_chat_settings(chat_id)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [chat_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE chat_settings SET {set_clause} WHERE chat_id=?", values)
    return True


def get_task_key(task_id, chat_id):
    """Возвращает строку вида JV-123."""
    settings = get_chat_settings(chat_id)
    return f"{settings['key_prefix']}-{task_id}"


# ═══════════════════════════════════════════════════════════════
#  АРХИВ
# ═══════════════════════════════════════════════════════════════

def archive_task(task_id, user_id):
    """Архивирует задачу — не удаляет, но скрывает из основных списков."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET archived_at=? WHERE id=?", (now, task_id))
    return True


def restore_task(task_id):
    """Восстанавливает задачу из архива."""
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET archived_at=NULL WHERE id=?", (task_id,))
    return True


def get_archived_tasks(chat_id):
    """Список архивных задач чата."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks WHERE chat_id=? AND archived_at IS NOT NULL
               ORDER BY archived_at DESC""", (chat_id,)).fetchall()
        return [_enrich(conn, r) for r in rows]


def auto_archive_old_done(chat_id=None):
    """Автоматически архивирует выполненные задачи старше N дней."""
    if chat_id:
        settings = get_chat_settings(chat_id)
        days = settings.get("auto_archive_days", 7)
        chats = [(chat_id, days)]
    else:
        with get_connection() as conn:
            rows = conn.execute("SELECT chat_id, auto_archive_days FROM chat_settings").fetchall()
            chats = [(r["chat_id"], r["auto_archive_days"]) for r in rows]
    
    total = 0
    for cid, days in chats:
        if days <= 0:
            continue
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with get_connection() as conn:
            result = conn.execute(
                """UPDATE tasks SET archived_at=? 
                   WHERE chat_id=? AND status='done' 
                   AND completed_at IS NOT NULL AND completed_at<?
                   AND archived_at IS NULL""",
                (datetime.now().isoformat(), cid, cutoff))
            total += result.rowcount
    return total


# ═══════════════════════════════════════════════════════════════
#  ВЕБ-ХУКИ (Zapier / Make.com / любые URL)
# ═══════════════════════════════════════════════════════════════

WEBHOOK_EVENTS = [
    "task.created", "task.updated", "task.completed", "task.deleted",
    "task.archived", "comment.added", "assignee.added", "subtask.added"
]


def create_webhook(chat_id, name, url, events, user_id):
    """Создаёт веб-хук. events — список или строка через запятую."""
    if isinstance(events, list):
        events = ",".join(events)
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO webhooks (chat_id, name, url, events, created_by, created_at)
               VALUES (?,?,?,?,?,?)""",
            (chat_id, name, url, events, user_id, now))
        return cur.lastrowid


def get_webhooks(chat_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM webhooks WHERE chat_id=? ORDER BY created_at DESC",
            (chat_id,)).fetchall()
        return [dict(r) for r in rows]


def get_active_webhooks_for_event(chat_id, event):
    """Возвращает активные веб-хуки которые подписаны на это событие."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM webhooks WHERE chat_id=? AND is_active=1
               AND (events LIKE ? OR events LIKE ? OR events='*')""",
            (chat_id, f"%{event}%", f"%*%")).fetchall()
        return [dict(r) for r in rows if event in (r["events"] or "") or r["events"] == "*"]


def update_webhook(webhook_id, **fields):
    allowed = {"name", "url", "events", "is_active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "events" in updates and isinstance(updates["events"], list):
        updates["events"] = ",".join(updates["events"])
    if not updates:
        return False
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [webhook_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE webhooks SET {set_clause} WHERE id=?", values)
    return True


def delete_webhook(webhook_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
    return True


def log_webhook_trigger(webhook_id, success=True, error=""):
    """Логирует факт срабатывания веб-хука."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if success:
            conn.execute(
                """UPDATE webhooks SET last_triggered_at=?, trigger_count=trigger_count+1
                   WHERE id=?""", (now, webhook_id))
        else:
            conn.execute(
                """UPDATE webhooks SET error_count=error_count+1, last_error=?
                   WHERE id=?""", (error[:500], webhook_id))


# ═══════════════════════════════════════════════════════════════
#  ОБНОВЛЁННЫЙ ПОИСК С ФИЛЬТРАМИ И СОРТИРОВКОЙ
# ═══════════════════════════════════════════════════════════════

def get_tasks_filtered(chat_id, filters=None, sort_by="created_at", sort_dir="desc",
                       include_archived=False, limit=500):
    """Универсальный поиск задач с фильтрами.
    filters: dict — может содержать: status, priority, category, assignee_id, search, project_id, type
    """
    filters = filters or {}
    where = ["chat_id=?"]
    params = [chat_id]
    
    if not include_archived:
        where.append("archived_at IS NULL")
    
    if filters.get("status"):
        where.append("status=?")
        params.append(filters["status"])
    if filters.get("priority"):
        where.append("priority=?")
        params.append(filters["priority"])
    if filters.get("category"):
        where.append("category=?")
        params.append(filters["category"])
    if filters.get("project_id"):
        where.append("project_id=?")
        params.append(filters["project_id"])
    if filters.get("type"):
        where.append("task_type=?")
        params.append(filters["type"])
    if filters.get("search"):
        q = f"%{filters['search']}%"
        where.append("(title LIKE ? OR description LIKE ? OR tags LIKE ?)")
        params.extend([q, q, q])
    
    valid_sort = {"created_at", "deadline", "priority", "title", "status", "id"}
    if sort_by not in valid_sort:
        sort_by = "created_at"
    sort_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"
    
    query = f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY {sort_by} {sort_dir} LIMIT ?"
    params.append(limit)
    
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_enrich(conn, r) for r in rows]
