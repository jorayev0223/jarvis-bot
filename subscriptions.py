"""
J.A.R.V.I.S. — Система подписок
Тарифы: Free, Basic, Business
Оплата через Telegram Stars
"""

import os
from datetime import datetime, timedelta
from database import get_connection

# ═══════════════════════════════════════════════════════════════
#  ТАРИФНЫЕ ПЛАНЫ
# ═══════════════════════════════════════════════════════════════

PLANS = {
    "free": {
        "name": "Free",
        "emoji": "🆓",
        "price_stars": 0,
        "price_label": "Бесплатно",
        "max_users": 3,
        "max_tasks": 20,
        "max_projects": 1,
        "ai_reports": False,
        "web_dashboard": False,
        "recurring_tasks": False,
        "file_attachments": False,
        "morning_digest": False,
        "personal_notifications": False,
        "subtasks": False,
    },
    "basic": {
        "name": "Basic",
        "emoji": "⭐",
        "price_stars": 250,       # ~$5
        "price_label": "250 ⭐ / месяц (~$5)",
        "max_users": 15,
        "max_tasks": 0,           # 0 = безлимит
        "max_projects": 10,
        "ai_reports": True,
        "web_dashboard": True,
        "recurring_tasks": True,
        "file_attachments": True,
        "morning_digest": True,
        "personal_notifications": True,
        "subtasks": True,
    },
    "business": {
        "name": "Business",
        "emoji": "💎",
        "price_stars": 500,       # ~$10
        "price_label": "500 ⭐ / месяц (~$10)",
        "max_users": 50,
        "max_tasks": 0,
        "max_projects": 0,
        "ai_reports": True,
        "web_dashboard": True,
        "recurring_tasks": True,
        "file_attachments": True,
        "morning_digest": True,
        "personal_notifications": True,
        "subtasks": True,
    },
}


# ═══════════════════════════════════════════════════════════════
#  ТАБЛИЦЫ
# ═══════════════════════════════════════════════════════════════

def init_subscription_tables():
    with get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE NOT NULL,
            plan TEXT DEFAULT 'free',
            started_at TEXT NOT NULL,
            expires_at TEXT DEFAULT NULL,
            auto_renew INTEGER DEFAULT 0,
            payment_provider TEXT DEFAULT '',
            total_paid INTEGER DEFAULT 0
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS payment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount_stars INTEGER DEFAULT 0,
            telegram_payment_id TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""")


# ═══════════════════════════════════════════════════════════════
#  ПОДПИСКИ
# ═══════════════════════════════════════════════════════════════

def get_subscription(chat_id):
    """Получает текущую подписку чата."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            sub = dict(row)
            # Проверяем срок действия
            if sub.get("expires_at") and sub["plan"] != "free":
                if datetime.now() > datetime.fromisoformat(sub["expires_at"]):
                    # Подписка истекла — откатываем на free
                    conn.execute(
                        "UPDATE subscriptions SET plan='free' WHERE chat_id=?",
                        (chat_id,))
                    sub["plan"] = "free"
            return sub
        # Нет записи — создаём free
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO subscriptions (chat_id, plan, started_at) VALUES (?,?,?)",
            (chat_id, "free", now))
        return {"chat_id": chat_id, "plan": "free", "started_at": now,
                "expires_at": None, "auto_renew": 0}


def get_plan(chat_id):
    """Возвращает данные тарифа для чата."""
    sub = get_subscription(chat_id)
    plan_name = sub.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"]).copy()
    plan["plan_id"] = plan_name
    plan["expires_at"] = sub.get("expires_at")
    plan["is_active"] = True
    if plan_name != "free" and sub.get("expires_at"):
        plan["is_active"] = datetime.now() < datetime.fromisoformat(sub["expires_at"])
    return plan


def activate_plan(chat_id, plan_name, user_id=0, months=1,
                   payment_id="", amount_stars=0):
    """Активирует подписку."""
    if plan_name not in PLANS:
        return False
    now = datetime.now()
    expires = now + timedelta(days=30 * months)
    with get_connection() as conn:
        sub = conn.execute(
            "SELECT id, expires_at FROM subscriptions WHERE chat_id=?",
            (chat_id,)).fetchone()
        if sub:
            # Продлеваем от текущей даты окончания если подписка ещё активна
            if sub["expires_at"]:
                old_exp = datetime.fromisoformat(sub["expires_at"])
                if old_exp > now:
                    expires = old_exp + timedelta(days=30 * months)
            conn.execute(
                """UPDATE subscriptions SET plan=?, started_at=?,
                   expires_at=?, total_paid=total_paid+?
                   WHERE chat_id=?""",
                (plan_name, now.isoformat(), expires.isoformat(),
                 amount_stars, chat_id))
        else:
            conn.execute(
                """INSERT INTO subscriptions
                   (chat_id, plan, started_at, expires_at, total_paid)
                   VALUES (?,?,?,?,?)""",
                (chat_id, plan_name, now.isoformat(),
                 expires.isoformat(), amount_stars))
        # Записываем в историю
        conn.execute(
            """INSERT INTO payment_history
               (chat_id, user_id, plan, amount_stars, telegram_payment_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (chat_id, user_id, plan_name, amount_stars,
             payment_id, now.isoformat()))
    return True


def cancel_plan(chat_id):
    """Отменяет подписку (переводит на free)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE subscriptions SET plan='free', expires_at=NULL WHERE chat_id=?",
            (chat_id,))


# ═══════════════════════════════════════════════════════════════
#  ПРОВЕРКА ЛИМИТОВ
# ═══════════════════════════════════════════════════════════════

def check_task_limit(chat_id):
    """Проверяет, можно ли создать ещё задачу."""
    plan = get_plan(chat_id)
    if plan["max_tasks"] == 0:  # безлимит
        return True, plan
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE chat_id=? AND status='active'",
            (chat_id,)).fetchone()[0]
    if count >= plan["max_tasks"]:
        return False, plan
    return True, plan


def check_user_limit(chat_id):
    """Проверяет лимит пользователей."""
    plan = get_plan(chat_id)
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(DISTINCT creator_id) FROM tasks WHERE chat_id=?",
            (chat_id,)).fetchone()[0]
    if count >= plan["max_users"]:
        return False, plan
    return True, plan


def check_project_limit(chat_id):
    """Проверяет лимит проектов."""
    plan = get_plan(chat_id)
    if plan["max_projects"] == 0:
        return True, plan
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE chat_id=?",
            (chat_id,)).fetchone()[0]
    if count >= plan["max_projects"]:
        return False, plan
    return True, plan


def check_feature(chat_id, feature):
    """Проверяет доступность фичи: ai_reports, web_dashboard, recurring_tasks, etc."""
    plan = get_plan(chat_id)
    return plan.get(feature, False), plan


# ═══════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════

def format_plan_info(chat_id):
    """Красивый текст текущего тарифа."""
    plan = get_plan(chat_id)
    sub = get_subscription(chat_id)
    p = plan["plan_id"]

    lines = [f"{plan['emoji']} *Тариф: {plan['name']}*\n"]

    tasks_limit = "∞" if plan["max_tasks"] == 0 else str(plan["max_tasks"])
    users_limit = str(plan["max_users"])
    projects_limit = "∞" if plan["max_projects"] == 0 else str(plan["max_projects"])

    with get_connection() as conn:
        active_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE chat_id=? AND status='active'",
            (chat_id,)).fetchone()[0]

    lines.append(f"📋 Задачи: {active_tasks} / {tasks_limit}")
    lines.append(f"👥 Пользователи: до {users_limit}")
    lines.append(f"📁 Проекты: до {projects_limit}")
    lines.append("")

    features = [
        ("ai_reports", "🤖 AI-отчёты"),
        ("web_dashboard", "🖥 Веб-дашборд"),
        ("recurring_tasks", "🔄 Повторяющиеся задачи"),
        ("subtasks", "📝 Подзадачи"),
        ("file_attachments", "📎 Файлы"),
        ("morning_digest", "🌅 Утренний дайджест"),
        ("personal_notifications", "📩 Личные уведомления"),
    ]

    for key, label in features:
        icon = "✅" if plan.get(key) else "🔒"
        lines.append(f"  {icon} {label}")

    if plan.get("expires_at") and p != "free":
        exp = datetime.fromisoformat(plan["expires_at"])
        days_left = (exp - datetime.now()).days
        lines.append(f"\n⏳ Действует до: {exp.strftime('%d.%m.%Y')} ({days_left} дн.)")

    return "\n".join(lines)


def format_plans_comparison():
    """Сравнение тарифов для /upgrade."""
    lines = ["📊 *Тарифные планы J.A.R.V.I.S.*\n"]

    for pid, p in PLANS.items():
        lines.append(f"{p['emoji']} *{p['name']}* — {p['price_label']}")
        tasks = "∞" if p["max_tasks"] == 0 else str(p["max_tasks"])
        projects = "∞" if p["max_projects"] == 0 else str(p["max_projects"])
        lines.append(f"   👥 До {p['max_users']} чел. | 📋 {tasks} задач | 📁 {projects} проектов")
        feats = []
        if p["ai_reports"]:
            feats.append("AI-отчёты")
        if p["web_dashboard"]:
            feats.append("дашборд")
        if p["recurring_tasks"]:
            feats.append("повторы")
        if p["subtasks"]:
            feats.append("подзадачи")
        if p["personal_notifications"]:
            feats.append("уведомления")
        if feats:
            lines.append(f"   ✅ {', '.join(feats)}")
        lines.append("")

    return "\n".join(lines)


def get_upgrade_limit_message(plan, feature_name=""):
    """Сообщение когда лимит достигнут."""
    current = plan["plan_id"]
    if current == "free":
        next_plan = PLANS["basic"]
        return (
            f"🔒 *Лимит тарифа Free достигнут*\n\n"
            f"{feature_name}\n\n"
            f"Перейдите на ⭐ *Basic* за {next_plan['price_label']}:\n"
            f"/upgrade"
        )
    elif current == "basic":
        next_plan = PLANS["business"]
        return (
            f"🔒 *Лимит тарифа Basic достигнут*\n\n"
            f"{feature_name}\n\n"
            f"Перейдите на 💎 *Business* за {next_plan['price_label']}:\n"
            f"/upgrade"
        )
    else:
        return "Обратитесь в поддержку для расширения лимитов."
