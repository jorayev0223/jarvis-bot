"""
J.A.R.V.I.S. v5 — Web Dashboard API
Читает dashboard.html из той же папки. Подпапки не нужны.
"""

import os
import logging
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from datetime import datetime

import database as db

logger = logging.getLogger("jarvis.web")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "jarvis-secret-key")

PORT = int(os.environ.get("PORT", 8080))

# Загружаем HTML из файла рядом с web.py
_dir = os.path.dirname(os.path.abspath(__file__))
_html_path = os.path.join(_dir, "dashboard.html")
try:
    with open(_html_path, "r", encoding="utf-8") as f:
        DASHBOARD_HTML = f.read()
except FileNotFoundError:
    DASHBOARD_HTML = "<h1>J.A.R.V.I.S.</h1><p>dashboard.html not found</p>"


# ─── Middleware ───────────────────────────────────────────────

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


# ─── Страницы ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─── API: Auth ───────────────────────────────────────────────

@app.route("/api/auth/verify", methods=["POST"])
def verify_token():
    data = request.get_json() or {}
    token = data.get("token", "")
    token_data = db.validate_token(token)
    if not token_data:
        return jsonify({"valid": False, "error": "Токен недействителен"}), 401
    return jsonify({
        "valid": True,
        "user_id": token_data["user_id"],
        "username": token_data["username"],
        "chat_id": token_data["chat_id"],
        "expires_at": token_data["expires_at"]
    })

@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def logout():
    db.revoke_token(request.token)
    return jsonify({"success": True})


# ─── API: Tasks CRUD ─────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
@require_auth
def get_tasks():
    status = request.args.get("status")
    tasks = db.get_tasks_for_dashboard(request.user_id, request.chat_id, status)
    # Сериализуем для JSON
    clean = []
    for t in tasks:
        ct = dict(t)
        ct.pop("assignees", None)
        ct.pop("files", None)
        ct.pop("subtasks", None)
        clean.append(ct)
    return jsonify({"tasks": clean, "count": len(clean)})

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
    return jsonify({"success": True, "task_id": task_id}), 201

@app.route("/api/tasks/<int:task_id>", methods=["PUT", "PATCH"])
@require_auth
def update_task(task_id):
    data = request.get_json() or {}
    success = db.update_task_from_dashboard(task_id, request.user_id, **data)
    return jsonify({"success": success})

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@require_auth
def delete_task(task_id):
    db.delete_task_from_dashboard(task_id, request.user_id)
    return jsonify({"success": True})

@app.route("/api/tasks/<int:task_id>/move", methods=["POST"])
@require_auth
def move_task(task_id):
    data = request.get_json() or {}
    column = data.get("column", "todo")
    order = data.get("order", 0)
    db.reorder_kanban(request.user_id, task_id, column, order)
    return jsonify({"success": True})


# ─── API: Stats ──────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
@require_auth
def get_stats():
    stats = db.get_dashboard_stats(request.user_id, request.chat_id)
    return jsonify(stats)


# ─── API: Health ─────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "service": "J.A.R.V.I.S. v5"})


# ─── Запуск ──────────────────────────────────────────────────

def start_web():
    """Вызывается из bot.py в отдельном потоке."""
    db.init_db()
    logger.info(f"🌐 Dashboard на порту {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    app.run(host="0.0.0.0", port=PORT, debug=True)
