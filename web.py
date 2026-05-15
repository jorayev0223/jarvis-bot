"""
J.A.R.V.I.S. v5.3 — Web Dashboard API
Stage 1: архив, веб-хуки (Zapier/Make), настройки, фильтрация задач, ключи.
"""

import os
import logging
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from datetime import datetime

import database as db
import webhooks as wh

logger = logging.getLogger("jarvis.web")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "jarvis-secret-key")

PORT = int(os.environ.get("PORT", 8080))

_dir = os.path.dirname(os.path.abspath(__file__))
_html_path = os.path.join(_dir, "dashboard.html")
try:
    with open(_html_path, "r", encoding="utf-8") as f:
        DASHBOARD_HTML = f.read()
except FileNotFoundError:
    DASHBOARD_HTML = "<h1>J.A.R.V.I.S.</h1><p>dashboard.html not found</p>"


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif request.args.get("token"):
            token = request.args.get("token")
        if not token:
            return jsonify({"error": "Токен не предоставлен"}), 401
        token_data = db.validate_token(token)
        if not token_data:
            return jsonify({"error": "Токен недействителен или истёк"}), 401
        request.user_id = token_data["user_id"]
        request.chat_id = token_data["chat_id"]
        request.username = token_data["username"]
        request.token = token
        return f(*args, **kwargs)
    return decorated


def add_task_key(task, chat_id):
    if task and task.get("id"):
        settings = db.get_chat_settings(chat_id)
        task["key"] = f"{settings['key_prefix']}-{task['id']}"
    return task


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/dashboard")
def dashboard_page():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/auth/verify", methods=["POST"])
def verify_token():
    data = request.get_json() or {}
    token = data.get("token", "")
    token_data = db.validate_token(token)
    if not token_data:
        return jsonify({"valid": False, "error": "Токен недействителен"}), 401
    settings = db.get_chat_settings(token_data["chat_id"])
    return jsonify({
        "valid": True,
        "user_id": token_data["user_id"],
        "username": token_data["username"],
        "chat_id": token_data["chat_id"],
        "expires_at": token_data["expires_at"],
        "settings": settings
    })

@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def logout():
    db.revoke_token(request.token)
    return jsonify({"success": True})


# ─── Tasks ──────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
@require_auth
def get_tasks():
    include_archived = request.args.get("include_archived", "false").lower() == "true"
    archived_only = request.args.get("archived_only", "false").lower() == "true"
    if archived_only:
        tasks = db.get_archived_tasks(request.chat_id)
    else:
        tasks = db.get_tasks_for_dashboard(request.user_id, request.chat_id,
                                           include_archived=include_archived)
    tasks = [add_task_key(dict(t), request.chat_id) for t in tasks]
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.route("/api/tasks/<int:task_id>", methods=["GET"])
@require_auth
def get_task_detail(task_id):
    task = db.get_task(task_id)
    if not task:
        return jsonify({"error": "Задача не найдена"}), 404
    task["comments"] = db.get_comments(task_id, limit=100)
    if task.get("project_id"):
        task["project"] = db.get_project(task["project_id"])
    add_task_key(task, request.chat_id)
    return jsonify(task)


@app.route("/api/tasks", methods=["POST"])
@require_auth
def create_task():
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Название обязательно"}), 400
    task_id = db.create_task_from_dashboard(
        user_id=request.user_id, chat_id=request.chat_id,
        title=title, description=data.get("description", ""),
        priority=data.get("priority", "medium"),
        category=data.get("category", ""),
        deadline=data.get("deadline"),
        kanban_column=data.get("kanban_column", "todo"))
    extras = {k: v for k, v in data.items()
              if k in {"tags", "project_id", "start_date", "task_type"}}
    if extras:
        with db.get_connection() as conn:
            for k, v in extras.items():
                conn.execute(f"UPDATE tasks SET {k}=? WHERE id=?", (v, task_id))
    task = db.get_task(task_id)
    wh.trigger_event("task.created", request.chat_id, task=task)
    key = f"{db.get_chat_settings(request.chat_id)['key_prefix']}-{task_id}"
    return jsonify({"success": True, "task_id": task_id, "key": key}), 201


@app.route("/api/tasks/<int:task_id>", methods=["PUT", "PATCH"])
@require_auth
def update_task(task_id):
    data = request.get_json() or {}
    db.update_task_from_dashboard(task_id, request.user_id, **{
        k: v for k, v in data.items()
        if k in {"title", "description", "priority", "category", "deadline",
                 "status", "kanban_column", "kanban_order"}
    })
    if "tags" in data: db.update_tags(task_id, data["tags"])
    if "project_id" in data:
        db.set_project(task_id, data["project_id"] if data["project_id"] else None)
    extras = {k: v for k, v in data.items() if k in {"start_date", "task_type", "parent_id"}}
    if extras:
        with db.get_connection() as conn:
            for k, v in extras.items():
                conn.execute(f"UPDATE tasks SET {k}=? WHERE id=?", (v, task_id))
    task = db.get_task(task_id)
    if data.get("kanban_column") == "done" or data.get("status") == "done":
        wh.trigger_event("task.completed", request.chat_id, task=task)
    else:
        wh.trigger_event("task.updated", request.chat_id, task=task)
    return jsonify({"success": True})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@require_auth
def delete_task(task_id):
    task = db.get_task(task_id)
    wh.trigger_event("task.deleted", request.chat_id, task=task)
    db.delete_task_from_dashboard(task_id, request.user_id)
    return jsonify({"success": True})


@app.route("/api/tasks/<int:task_id>/move", methods=["POST"])
@require_auth
def move_task(task_id):
    data = request.get_json() or {}
    column = data.get("column", "todo")
    order = data.get("order", 0)
    db.reorder_kanban(request.user_id, task_id, column, order)
    task = db.get_task(task_id)
    if column == "done":
        wh.trigger_event("task.completed", request.chat_id, task=task)
    else:
        wh.trigger_event("task.updated", request.chat_id, task=task)
    return jsonify({"success": True})


# ─── Archive ────────────────────────────────────────────────

@app.route("/api/tasks/<int:task_id>/archive", methods=["POST"])
@require_auth
def archive_task(task_id):
    db.archive_task(task_id, request.user_id)
    task = db.get_task(task_id)
    wh.trigger_event("task.archived", request.chat_id, task=task)
    return jsonify({"success": True})


@app.route("/api/tasks/<int:task_id>/restore", methods=["POST"])
@require_auth
def restore_task(task_id):
    db.restore_task(task_id)
    return jsonify({"success": True})


@app.route("/api/archive/auto-cleanup", methods=["POST"])
@require_auth
def auto_archive():
    count = db.auto_archive_old_done(request.chat_id)
    return jsonify({"success": True, "archived": count})


# ─── Comments ───────────────────────────────────────────────

@app.route("/api/tasks/<int:task_id>/comments", methods=["GET"])
@require_auth
def get_task_comments(task_id):
    return jsonify({"comments": db.get_comments(task_id, limit=100)})


@app.route("/api/tasks/<int:task_id>/comments", methods=["POST"])
@require_auth
def add_task_comment(task_id):
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Текст обязателен"}), 400
    db.add_comment(task_id, request.user_id, request.username, text)
    task = db.get_task(task_id)
    wh.trigger_event("comment.added", request.chat_id, task=task,
                     extra={"comment": {"text": text, "author": request.username}})
    return jsonify({"success": True}), 201


@app.route("/api/comments/<int:comment_id>", methods=["DELETE"])
@require_auth
def delete_comment(comment_id):
    with db.get_connection() as conn:
        conn.execute("DELETE FROM comments WHERE id=? AND user_id=?",
                     (comment_id, request.user_id))
    return jsonify({"success": True})


# ─── Subtasks ───────────────────────────────────────────────

@app.route("/api/tasks/<int:task_id>/subtasks", methods=["GET"])
@require_auth
def get_task_subtasks(task_id):
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM subtasks WHERE task_id=? ORDER BY id",
                            (task_id,)).fetchall()
        return jsonify({"subtasks": [dict(r) for r in rows]})


@app.route("/api/tasks/<int:task_id>/subtasks", methods=["POST"])
@require_auth
def add_task_subtask(task_id):
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Название обязательно"}), 400
    db.add_subtask(task_id, title)
    task = db.get_task(task_id)
    wh.trigger_event("subtask.added", request.chat_id, task=task,
                     extra={"subtask": {"title": title}})
    return jsonify({"success": True}), 201


@app.route("/api/subtasks/<int:subtask_id>/toggle", methods=["POST"])
@require_auth
def toggle_subtask(subtask_id):
    new_state = db.toggle_subtask(subtask_id)
    return jsonify({"success": True, "done": new_state})


@app.route("/api/subtasks/<int:subtask_id>", methods=["DELETE"])
@require_auth
def delete_subtask(subtask_id):
    with db.get_connection() as conn:
        conn.execute("DELETE FROM subtasks WHERE id=?", (subtask_id,))
    return jsonify({"success": True})


# ─── Assignees ──────────────────────────────────────────────

@app.route("/api/tasks/<int:task_id>/assignees", methods=["GET"])
@require_auth
def get_task_assignees(task_id):
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_assignees WHERE task_id=?",
                            (task_id,)).fetchall()
        return jsonify({"assignees": [dict(r) for r in rows]})


@app.route("/api/tasks/<int:task_id>/assignees", methods=["POST"])
@require_auth
def add_task_assignee(task_id):
    data = request.get_json() or {}
    username = (data.get("username") or "").lstrip("@").strip()
    user_id = data.get("user_id", 0)
    first_name = data.get("first_name", "")
    if not username and not user_id:
        return jsonify({"error": "Укажите username или user_id"}), 400
    if not user_id and username:
        real_id = db.get_user_id_by_username(username)
        if real_id:
            user_id = real_id
    success = db.add_assignee(task_id, user_id or 0, username=username, first_name=first_name)
    task = db.get_task(task_id)
    wh.trigger_event("assignee.added", request.chat_id, task=task,
                     extra={"assignee": {"username": username, "user_id": user_id}})
    return jsonify({"success": success}), 201


@app.route("/api/tasks/<int:task_id>/assignees/<int:assignee_id>", methods=["DELETE"])
@require_auth
def remove_assignee(task_id, assignee_id):
    with db.get_connection() as conn:
        conn.execute("DELETE FROM task_assignees WHERE id=? AND task_id=?",
                     (assignee_id, task_id))
    return jsonify({"success": True})


# ─── Projects ───────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
@require_auth
def get_projects():
    return jsonify({"projects": db.get_projects(request.chat_id)})


@app.route("/api/projects", methods=["POST"])
@require_auth
def create_project():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Название обязательно"}), 400
    proj = db.create_project(
        chat_id=request.chat_id, name=name,
        description=data.get("description", ""),
        emoji=data.get("emoji", "📁"))
    return jsonify({"success": True, "project": proj}), 201


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
@require_auth
def delete_project(project_id):
    db.delete_project(project_id)
    return jsonify({"success": True})


# ─── Users ──────────────────────────────────────────────────

@app.route("/api/users/search", methods=["GET"])
@require_auth
def search_users():
    q = (request.args.get("q") or "").lower().strip()
    with db.get_connection() as conn:
        if q:
            rows = conn.execute(
                """SELECT user_id, username, first_name FROM known_users
                   WHERE LOWER(username) LIKE ? OR LOWER(first_name) LIKE ?
                   ORDER BY last_seen DESC LIMIT 20""",
                (f"%{q}%", f"%{q}%")).fetchall()
        else:
            rows = conn.execute(
                """SELECT user_id, username, first_name FROM known_users
                   ORDER BY last_seen DESC LIMIT 20""").fetchall()
        return jsonify({"users": [dict(r) for r in rows]})


# ─── Settings ───────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    return jsonify(db.get_chat_settings(request.chat_id))


@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    data = request.get_json() or {}
    db.update_chat_settings(request.chat_id, **data)
    return jsonify({"success": True, "settings": db.get_chat_settings(request.chat_id)})


# ─── Webhooks (Zapier/Make.com) ─────────────────────────────

@app.route("/api/webhooks", methods=["GET"])
@require_auth
def list_webhooks():
    return jsonify({
        "webhooks": db.get_webhooks(request.chat_id),
        "available_events": [
            "task.created", "task.updated", "task.completed", "task.deleted",
            "task.archived", "comment.added", "assignee.added", "subtask.added"
        ]
    })


@app.route("/api/webhooks", methods=["POST"])
@require_auth
def create_webhook():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    events = data.get("events", ["task.created", "task.completed"])
    if not name or not url:
        return jsonify({"error": "name и url обязательны"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL должен начинаться с http:// или https://"}), 400
    wid = db.create_webhook(request.chat_id, name, url, events, request.user_id)
    return jsonify({"success": True, "webhook_id": wid}), 201


@app.route("/api/webhooks/<int:webhook_id>", methods=["PUT"])
@require_auth
def update_webhook(webhook_id):
    data = request.get_json() or {}
    db.update_webhook(webhook_id, **data)
    return jsonify({"success": True})


@app.route("/api/webhooks/<int:webhook_id>", methods=["DELETE"])
@require_auth
def delete_webhook(webhook_id):
    db.delete_webhook(webhook_id)
    return jsonify({"success": True})


@app.route("/api/webhooks/test", methods=["POST"])
@require_auth
def test_webhook_endpoint():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL обязателен"}), 400
    return jsonify(wh.test_webhook(url))


# ─── Stats ──────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
@require_auth
def get_stats():
    return jsonify(db.get_dashboard_stats(request.user_id, request.chat_id))


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "service": "J.A.R.V.I.S. v5.3"})


def start_web():
    db.init_db()
    logger.info(f"🌐 Dashboard на порту {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    app.run(host="0.0.0.0", port=PORT, debug=True)
