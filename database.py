"""
J.A.R.V.I.S. v5 — Database
Все таблицы: задачи, подзадачи, комментарии, проекты, назначения,
файлы, статистика, токены дашборда, канбан.
"""

import sqlite3
import os
import secrets
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
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                   CASE WHEN deadline IS NOT NULL THEN 0 ELSE 1 END, deadline ASC""",
                (chat_id, project_id)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE chat_id=? AND status='active'
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                   CASE WHEN deadline IS NOT NULL THEN 0 ELSE 1 END, deadline ASC""",
                (chat_id,)).fetchall()
        return [_enrich(conn, r) for r in rows]

def get_all_tasks(chat_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id=? ORDER BY status ASC, created_at DESC",
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
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT id FROM task_assignees WHERE task_id=? AND user_id=?",
            (task_id, user_id)).fetchone()
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

def generate_dashboard_token(user_id, chat_id, username="", hours=72):
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

def get_tasks_for_dashboard(user_id, chat_id=None, status=None):
    with get_connection() as conn:
        query = "SELECT * FROM tasks WHERE (creator_id=? OR id IN (SELECT task_id FROM task_assignees WHERE user_id=?))"
        params = [user_id, user_id]
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
