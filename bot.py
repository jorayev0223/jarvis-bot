"""
J.A.R.V.I.S. v5.1 — Telegram Bot
Обновлено: уведомления каждому исполнителю лично,
отслеживание user_id, бессрочные токены.
"""

import os
import logging
import tempfile
import threading
import re
import json as json_module
from datetime import datetime, timedelta

import anthropic
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler, ContextTypes, filters,
)

import database as db
import subscriptions as subs
import webhooks as wh
from ai_assistant import extract_task_from_text, generate_status_report

# ─── Настройка ────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден!")
if not ANTHROPIC_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY не найден!")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger("jarvis")

PRIORITY_EMOJI = {"high": "🔴", "medium": "🔵", "low": "⚪"}
PRIORITY_LABEL = {"high": "КРИТИЧЕСКИЙ", "medium": "СТАНДАРТ", "low": "НИЗКИЙ"}


# ═══════════════════════════════════════════════════════════════
#  ОТСЛЕЖИВАНИЕ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════

def track_user_from_update(update: Update):
    """Запоминаем каждого пользователя для отправки личных уведомлений."""
    user = update.effective_user
    if user:
        db.track_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            chat_id=update.effective_chat.id if update.effective_chat else 0
        )


async def check_access(update: Update) -> bool:
    """Проверяет доступ. Возвращает True если доступ есть."""
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username or "" if user else ""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if db.check_access(user_id, chat_id, username):
        return True
    # Нет доступа — отправляем отказ
    try:
        await update.effective_message.reply_text(
            "🔒 *Доступ запрещён*\n\n"
            "Этот бот приватный. Обратитесь к администратору для получения доступа.",
            parse_mode="Markdown")
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
#  ОТПРАВКА УВЕДОМЛЕНИЙ
# ═══════════════════════════════════════════════════════════════

async def notify_assignees(context, task, exclude_user_id=None, message=""):
    """Отправляет уведомление всем исполнителям задачи в личку."""
    user_ids = db.get_all_notifiable_users_for_task(task["id"])
    for uid in user_ids:
        if uid == exclude_user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=message,
                parse_mode="Markdown"
            )
            logger.info(f"📩 Уведомление отправлено user_id={uid}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить user_id={uid}: {e}")


async def notify_single_user(context, user_id=None, username="", message=""):
    """Отправляет уведомление конкретному пользователю."""
    target_id = user_id
    if (not target_id or target_id == 0) and username:
        target_id = db.get_user_id_by_username(username)
    if not target_id:
        return False
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=message,
            parse_mode="Markdown"
        )
        return True
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить user_id={target_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════

def format_task(task, index=None):
    p = task.get("priority", "medium")
    emoji = PRIORITY_EMOJI.get(p, "🔵")
    label = PRIORITY_LABEL.get(p, "СТАНДАРТ")
    status = "✅" if task.get("status") == "done" else emoji

    header = f"{status} "
    if index is not None:
        header += f"*{index}.* "
    header += f"*{task['title']}*  `#{task['id']}`"

    lines = [header]
    if task.get("description"):
        lines.append(f"   _{task['description']}_")
    lines.append(f"   Приоритет: {label}")
    if task.get("category"):
        lines.append(f"   Категория: {task['category']}")
    if task.get("tags"):
        lines.append(f"   🏷 {task['tags']}")
    if task.get("deadline"):
        dl = datetime.fromisoformat(task["deadline"])
        dl_str = dl.strftime("%d.%m.%Y %H:%M")
        if task.get("status") != "done" and dl < datetime.now():
            lines.append(f"   ⚠️ *ПРОСРОЧЕНО:* {dl_str}")
        else:
            lines.append(f"   ⏰ Дедлайн: {dl_str}")
    if task.get("recurrence"):
        rec_map = {"daily": "ежедневно", "weekly": "еженедельно", "monthly": "ежемесячно"}
        lines.append(f"   🔄 {rec_map.get(task['recurrence'], task['recurrence'])}")
    if task.get("assignees"):
        names = [f"@{a['username']}" if a.get("username") else a.get("first_name", "?")
                 for a in task["assignees"]]
        lines.append(f"   👤 {', '.join(names)}")
    if task.get("subtasks"):
        done_c = sum(1 for s in task["subtasks"] if s["done"])
        total_c = len(task["subtasks"])
        lines.append(f"   📝 Подзадачи: {done_c}/{total_c}")
    if task.get("comment_count", 0) > 0:
        lines.append(f"   💬 Комментариев: {task['comment_count']}")
    if task.get("project_id"):
        proj = db.get_project(task["project_id"])
        if proj:
            lines.append(f"   📁 {proj['emoji']} {proj['name']}")
    if task.get("files"):
        lines.append(f"   📎 Файлов: {len(task['files'])}")

    return "\n".join(lines)


def task_buttons(task_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅Готово", callback_data=f"done_{task_id}"),
         InlineKeyboardButton("🗑Удалить", callback_data=f"del_{task_id}")],
        [InlineKeyboardButton("+1ч", callback_data=f"post_1h_{task_id}"),
         InlineKeyboardButton("+3ч", callback_data=f"post_3h_{task_id}"),
         InlineKeyboardButton("+1д", callback_data=f"post_1d_{task_id}")],
        [InlineKeyboardButton("👤Назначить", callback_data=f"assign_{task_id}"),
         InlineKeyboardButton("📎Файл", callback_data=f"file_{task_id}")],
        [InlineKeyboardButton("📝Подзадачи", callback_data=f"subs_{task_id}"),
         InlineKeyboardButton("💬Комменты", callback_data=f"comms_{task_id}")],
        [InlineKeyboardButton("🏷Теги", callback_data=f"tags_{task_id}"),
         InlineKeyboardButton("📁Проект", callback_data=f"setprj_{task_id}")],
    ])


def subtask_btns(task_id, subtasks):
    rows = []
    for s in subtasks:
        icon = "✅" if s["done"] else "⬜"
        rows.append([InlineKeyboardButton(f"{icon} {s['title']}", callback_data=f"togsub_{s['id']}")])
    rows.append([
        InlineKeyboardButton("➕ Добавить", callback_data=f"addsub_{task_id}"),
        InlineKeyboardButton("⬅️ Назад", callback_data=f"view_{task_id}")
    ])
    return InlineKeyboardMarkup(rows)


def project_btns(chat_id, task_id):
    projects = db.get_projects(chat_id)
    if not projects:
        return None
    rows = [[InlineKeyboardButton(f"{p['emoji']} {p['name']}", callback_data=f"prj_{task_id}_{p['id']}")]
            for p in projects]
    rows.append([InlineKeyboardButton("❌ Без проекта", callback_data=f"prj_{task_id}_0")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_{task_id}")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update):
        return
    text = (
        "🤖 *J.A.R.V.I.S. v5 активирован!*\n\n"
        "Я ваш персональный ИИ-ассистент.\n\n"
        "📝 Просто напишите или отправьте голосовое — я создам задачу.\n"
        "Пример: _Напомни завтра в 10 позвонить в банк_\n\n"
        "Команды:\n"
        "/tasks — активные задачи\n"
        "/all — все задачи\n"
        "/my — мои задачи\n"
        "/done N — выполнить задачу\n"
        "/delete N — удалить\n"
        "/search слово — поиск\n"
        "/stats — статистика\n"
        "/projects — проекты\n"
        "/newproject Имя — создать проект\n"
        "/dashboard — веб-дашборд\n"
        "/plan — текущий тариф\n"
        "/upgrade — улучшить тариф\n"
        "/help — помощь"
    )
    # Если админ — показываем ещё админ-команды
    if db.is_admin(update.effective_user.id):
        text += (
            "\n\n🔐 *Админ-команды:*\n"
            "/adduser @username — дать доступ\n"
            "/removeuser @username — забрать доступ\n"
            "/users — список пользователей\n"
            "/addchat — разрешить эту группу\n"
            "/removechat — запретить эту группу\n"
            "/myid — узнать свой ID"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update):
        return
    await cmd_start(update, context)

# ─── Админ-команды ────────────────────────────────────────────

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает user_id и chat_id — полезно для настройки ADMIN_IDS."""
    track_user_from_update(update)
    user = update.effective_user
    chat = update.effective_chat
    await update.message.reply_text(
        f"👤 Ваш user_id: {user.id}\n"
        f"💬 chat_id: {chat.id}\n"
        f"📛 username: @{user.username or '—'}")

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    if not context.args:
        await update.message.reply_text(
            "Укажите @username или user\\_id:\n"
            "/adduser @username\n/adduser 123456789",
            parse_mode="Markdown")
        return
    arg = context.args[0]
    if arg.startswith("@"):
        username = arg[1:]
        real_id = db.get_user_id_by_username(username)
        if real_id:
            db.add_allowed_user(real_id, username=username, added_by=update.effective_user.id)
            await update.message.reply_text(
                f"✅ Пользователь @{username} (ID: `{real_id}`) добавлен!", parse_mode="Markdown")
        else:
            db.add_allowed_user(0, username=username, added_by=update.effective_user.id)
            await update.message.reply_text(
                f"⚠️ @{username} ещё не писал боту, поэтому ID неизвестен.\n"
                f"Пользователь добавлен по username. Когда он напишет /start, бот его узнает.\n\n"
                f"Или укажите user\\_id напрямую: /adduser 123456789",
                parse_mode="Markdown")
    else:
        try:
            uid = int(arg)
            db.add_allowed_user(uid, added_by=update.effective_user.id)
            await update.message.reply_text(f"✅ Пользователь ID `{uid}` добавлен!", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Укажите @username или числовой user\\_id.", parse_mode="Markdown")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    if not context.args:
        await update.message.reply_text("Укажите @username или user\\_id: /removeuser @username", parse_mode="Markdown")
        return
    arg = context.args[0]
    if arg.startswith("@"):
        uid = db.get_user_id_by_username(arg[1:])
        if uid:
            db.remove_allowed_user(uid)
            await update.message.reply_text(f"🗑 Доступ @{arg[1:]} отозван.", parse_mode="Markdown")
        else:
            await update.message.reply_text("Пользователь не найден.")
    else:
        try:
            uid = int(arg)
            db.remove_allowed_user(uid)
            await update.message.reply_text(f"🗑 Доступ ID `{uid}` отозван.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Укажите @username или user\\_id.", parse_mode="Markdown")

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    users = db.get_allowed_users()
    chats = db.get_allowed_chats()
    lines = ["🔐 *Управление доступом:*\n"]
    lines.append(f"*Пользователи ({len(users)}):*")
    if users:
        for u in users:
            name = f"@{u['username']}" if u.get('username') else f"ID: {u['user_id']}"
            lines.append(f"  ✅ {name}")
    else:
        lines.append("  _Список пуст_")
    lines.append(f"\n*Группы ({len(chats)}):*")
    if chats:
        for c in chats:
            lines.append(f"  ✅ {c.get('chat_title') or c['chat_id']}")
    else:
        lines.append("  _Список пуст_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_addchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    chat = update.effective_chat
    db.add_allowed_chat(chat.id, chat.title or "Личный чат", update.effective_user.id)
    await update.message.reply_text(
        f"✅ Чат *{chat.title or 'Личный чат'}* (ID: `{chat.id}`) разрешён!",
        parse_mode="Markdown")

async def cmd_removechat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    chat = update.effective_chat
    db.remove_allowed_chat(chat.id)
    await update.message.reply_text(f"🗑 Чат `{chat.id}` удалён из разрешённых.", parse_mode="Markdown")

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    tasks = db.get_active_tasks(update.effective_chat.id)
    if not tasks:
        await update.message.reply_text("✨ Нет активных задач, сэр.")
        return
    report = generate_status_report(claude, [{"title": t["title"], "priority": t["priority"],
              "deadline": t.get("deadline"), "status": t["status"]} for t in tasks[:10]])
    lines = [f"🤖 _{report}_\n\n📋 *Активные задачи ({len(tasks)}):*\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    tasks = db.get_all_tasks(update.effective_chat.id)
    if not tasks:
        await update.message.reply_text("📭 Задач нет.")
        return
    lines = [f"📋 *Все задачи ({len(tasks)}):*\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))
        lines.append("")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n_...список обрезан_"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    tasks = db.get_user_tasks(update.effective_chat.id, update.effective_user.id)
    if not tasks:
        await update.message.reply_text("✨ У вас нет назначенных задач.")
        return
    lines = [f"👤 *Ваши задачи ({len(tasks)}):*\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    if not context.args:
        await update.message.reply_text("Укажите номер: /done 1")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Укажите номер задачи.")
        return
    task = db.complete_task(task_id, update.effective_user.id, update.effective_chat.id)
    if task:
        # Веб-хук: задача выполнена
        wh.trigger_event("task.completed", update.effective_chat.id, task=task)
        await update.message.reply_text(f"✅ Задача *#{task_id}* выполнена!", parse_mode="Markdown")
        # Уведомляем исполнителей
        user_name = update.effective_user.first_name or "Кто-то"
        await notify_assignees(context, task, exclude_user_id=update.effective_user.id,
            message=f"✅ *{user_name}* выполнил задачу *#{task_id}*: {task['title']}")
    else:
        await update.message.reply_text("❌ Задача не найдена.")

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    if not context.args:
        await update.message.reply_text("Укажите номер: /delete 1")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Укажите номер задачи.")
        return
    # Получаем задачу для веб-хука ДО удаления
    task = db.get_task(task_id)
    if db.delete_task(task_id, update.effective_user.id, update.effective_chat.id):
        if task:
            wh.trigger_event("task.deleted", update.effective_chat.id, task=task)
        await update.message.reply_text(f"🗑 Задача *#{task_id}* удалена.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Задача не найдена.")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    count = db.clear_done_tasks(update.effective_chat.id)
    await update.message.reply_text(f"🧹 Удалено выполненных задач: {count}")

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    if not context.args:
        await update.message.reply_text("Укажите запрос: /search отчёт")
        return
    query = " ".join(context.args)
    tasks = db.search_tasks(update.effective_chat.id, query)
    if not tasks:
        await update.message.reply_text(f"🔍 По запросу «{query}» ничего не найдено.")
        return
    lines = [f"🔍 *Результаты «{query}» ({len(tasks)}):*\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    stats = db.get_stats(update.effective_chat.id)
    if not stats:
        await update.message.reply_text("📊 Статистика пуста.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["📊 *Статистика команды:*\n"]
    for i, s in enumerate(stats[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = s.get("user_name") or s.get("username") or "?"
        lines.append(
            f"{medal} *{name}*: создано {s['tasks_created']}, "
            f"выполнено {s['tasks_completed']}, назначено {s['tasks_assigned']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    projects = db.get_projects(update.effective_chat.id)
    if not projects:
        await update.message.reply_text("📁 Нет проектов. Создайте: /newproject Название")
        return
    lines = ["📁 *Проекты:*\n"]
    for p in projects:
        lines.append(f"{p['emoji']} *{p['name']}* (ID: {p['id']})")
        if p.get("description"):
            lines.append(f"   _{p['description']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_newproject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    if not context.args:
        await update.message.reply_text("Укажите название: /newproject Маркетинг")
        return
    name = " ".join(context.args)
    proj = db.create_project(update.effective_chat.id, name)
    await update.message.reply_text(
        f"📁 Проект *{name}* создан! (ID: {proj['id']})", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  ДАШБОРД
# ═══════════════════════════════════════════════════════════════

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = user.username or user.first_name or "Agent"

    token = db.generate_dashboard_token(
        user_id=user.id, chat_id=chat_id, username=username, hours=876000)

    dashboard_url = os.environ.get(
        "DASHBOARD_URL", "https://worker-production-6faa.up.railway.app")

    text = (
        f"🖥 *J.A.R.V.I.S. Dashboard*\n\n"
        f"Ваш токен доступа, сэр:\n\n"
        f"`{token}`\n\n"
        f"🔗 Откройте: {dashboard_url}\n\n"
        f"⏳ Токен бессрочный\n"
        f"🔒 Привязан к этому чату\n\n"
        f"_Скопируйте токен и вставьте на странице._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
#  CALLBACK КНОПКИ
# ═══════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    user = q.from_user

    # Отслеживаем пользователя
    db.track_user(user.id, user.username or "", user.first_name or "", q.message.chat_id)

    # Проверка доступа
    if not db.check_access(user.id, q.message.chat_id, user.username or ""):
        return

    if d.startswith("buy_"):
        await handle_buy_callback(update, context)
        return

    if d.startswith("done_"):
        tid = int(d.split("_")[1])
        task = db.complete_task(tid, user.id, q.message.chat_id)
        if task:
            wh.trigger_event("task.completed", q.message.chat_id, task=task)
            await q.edit_message_text(f"✅ Задача *#{tid}* выполнена!\n\n{format_task(task)}", parse_mode="Markdown")
            user_name = user.first_name or "Кто-то"
            await notify_assignees(context, task, exclude_user_id=user.id,
                message=f"✅ *{user_name}* выполнил задачу *#{tid}*: {task['title']}")

    elif d.startswith("del_"):
        tid = int(d.split("_")[1])
        task = db.get_task(tid)
        if db.delete_task(tid, user.id, q.message.chat_id):
            if task:
                wh.trigger_event("task.deleted", q.message.chat_id, task=task)
            await q.edit_message_text(f"🗑 Задача *#{tid}* удалена.", parse_mode="Markdown")

    elif d.startswith("post_"):
        parts = d.split("_")
        delta_str = parts[1]
        tid = int(parts[2])
        deltas = {"1h": timedelta(hours=1), "3h": timedelta(hours=3), "1d": timedelta(days=1)}
        delta = deltas.get(delta_str, timedelta(hours=1))
        task = db.postpone_task(tid, user.id, delta)
        if task:
            await q.edit_message_text(
                f"⏳ Дедлайн перенесён!\n\n{format_task(task)}", parse_mode="Markdown",
                reply_markup=task_buttons(tid))

    elif d.startswith("assign_"):
        tid = int(d.split("_")[1])
        context.user_data["await_assign"] = tid
        await q.edit_message_text(
            f"👤 Задача *#{tid}*\nОтправьте @username или перешлите сообщение пользователя.",
            parse_mode="Markdown")

    elif d.startswith("file_"):
        tid = int(d.split("_")[1])
        context.user_data["await_file"] = tid
        await q.edit_message_text(
            f"📎 Задача *#{tid}*\nОтправьте файл или фото.", parse_mode="Markdown")

    elif d.startswith("subs_"):
        tid = int(d.split("_")[1])
        t = db.get_task(tid)
        if t:
            subs = t.get("subtasks", [])
            done_c = sum(1 for s in subs if s["done"])
            header = f"📝 *Подзадачи #{tid}* ({done_c}/{len(subs)})\n_{t['title']}_"
            await q.edit_message_text(header, parse_mode="Markdown",
                                       reply_markup=subtask_btns(tid, subs))

    elif d.startswith("addsub_"):
        tid = int(d.split("_")[1])
        context.user_data["await_subtask"] = tid
        await q.edit_message_text(
            f"📝 Задача *#{tid}*\nНапишите подзадачу (или несколько через Enter).",
            parse_mode="Markdown")

    elif d.startswith("togsub_"):
        sid = int(d.split("_")[1])
        db.toggle_subtask(sid)
        with db.get_connection() as conn:
            row = conn.execute("SELECT task_id FROM subtasks WHERE id=?", (sid,)).fetchone()
        if row:
            tid = row["task_id"]
            t = db.get_task(tid)
            subs = t.get("subtasks", [])
            done_c = sum(1 for s in subs if s["done"])
            header = f"📝 *Подзадачи #{tid}* ({done_c}/{len(subs)})\n_{t['title']}_"
            await q.edit_message_text(header, parse_mode="Markdown",
                                       reply_markup=subtask_btns(tid, subs))

    elif d.startswith("comms_"):
        tid = int(d.split("_")[1])
        comments = db.get_comments(tid)
        t = db.get_task(tid)
        lines = [f"💬 *Комментарии к #{tid}*\n_{t['title']}_\n"]
        if comments:
            for c in reversed(comments):
                dt = datetime.fromisoformat(c["created_at"]).strftime("%d.%m %H:%M")
                lines.append(f"*{c['user_name']}* ({dt}):\n{c['text']}\n")
        else:
            lines.append("_Пока нет комментариев._")
        lines.append("\n✏️ Отправьте сообщение чтобы добавить комментарий.")
        context.user_data["await_comment"] = tid
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"view_{tid}")]])
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)

    elif d.startswith("tags_"):
        tid = int(d.split("_")[1])
        context.user_data["await_tags"] = tid
        t = db.get_task(tid)
        cur_tags = t.get("tags", "") if t else ""
        await q.edit_message_text(
            f"🏷 Задача *#{tid}*\nТеги: _{cur_tags or 'нет'}_\n\nНапишите теги через запятую:",
            parse_mode="Markdown")

    elif d.startswith("setprj_"):
        tid = int(d.split("_")[1])
        kb = project_btns(q.message.chat_id, tid)
        if kb:
            await q.edit_message_text(
                f"📁 Выберите проект для задачи *#{tid}*:",
                parse_mode="Markdown", reply_markup=kb)
        else:
            await q.edit_message_text(
                "📁 Нет проектов. Создайте: /newproject Название", parse_mode="Markdown")

    elif d.startswith("prj_"):
        parts = d.split("_")
        tid = int(parts[1])
        pid = int(parts[2])
        db.set_project(tid, pid if pid != 0 else None)
        t = db.get_task(tid)
        await q.edit_message_text(
            f"📁 Проект обновлён!\n\n{format_task(t)}", parse_mode="Markdown",
            reply_markup=task_buttons(tid))

    elif d.startswith("view_"):
        tid = int(d.split("_")[1])
        t = db.get_task(tid)
        if t:
            await q.edit_message_text(format_task(t), parse_mode="Markdown",
                                       reply_markup=task_buttons(tid))


# ═══════════════════════════════════════════════════════════════
#  ГОЛОСОВЫЕ СООБЩЕНИЯ
# ═══════════════════════════════════════════════════════════════

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    processing = await update.message.reply_text("🎤 Обрабатываю голосовое сообщение...")

    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            ogg_path = f.name
        await file.download_to_drive(ogg_path)

        wav_path = ogg_path.replace(".ogg", ".wav")
        import subprocess
        subprocess.run(["ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1",
                         wav_path, "-y"], capture_output=True)

        import speech_recognition as sr
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio, language="ru-RU")

        os.unlink(ogg_path)
        os.unlink(wav_path)

        await processing.edit_text(f"🎤 Распознано: _{text}_\n\n⏳ Анализирую...", parse_mode="Markdown")
        await _process_task_text(update, context, text, processing)

    except Exception as e:
        logger.error(f"Ошибка голосового: {e}")
        await processing.edit_text("❌ Не удалось распознать. Попробуйте текстом.")


# ═══════════════════════════════════════════════════════════════
#  ТЕКСТОВЫЕ СООБЩЕНИЯ
# ═══════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    text = update.message.text.strip()
    if not text:
        return

    # Проверяем ожидающие действия
    if context.user_data.get("await_subtask"):
        tid = context.user_data.pop("await_subtask")
        added_titles = []
        for line in text.split("\n"):
            line = line.strip()
            if line:
                db.add_subtask(tid, line)
                added_titles.append(line)
        t = db.get_task(tid)
        # Веб-хук на каждую добавленную подзадачу
        for title in added_titles:
            wh.trigger_event("subtask.added", update.effective_chat.id, task=t,
                             extra={"subtask": {"title": title}})
        subtasks_list = t.get("subtasks", [])
        done_c = sum(1 for s in subtasks_list if s["done"])
        await update.message.reply_text(
            f"📝 *Подзадачи #{tid}* ({done_c}/{len(subtasks_list)})\n_{t['title']}_",
            parse_mode="Markdown", reply_markup=subtask_btns(tid, subtasks_list))
        return

    if context.user_data.get("await_comment"):
        tid = context.user_data.pop("await_comment")
        name = update.effective_user.first_name or update.effective_user.username or "?"
        db.add_comment(tid, update.effective_user.id, name, text)
        # Веб-хук: комментарий добавлен
        task = db.get_task(tid)
        if task:
            wh.trigger_event("comment.added", update.effective_chat.id, task=task,
                             extra={"comment": {"text": text, "author": name}})
        await update.message.reply_text(f"💬 Комментарий добавлен к задаче *#{tid}*", parse_mode="Markdown")
        # Уведомляем остальных
        if task:
            await notify_assignees(context, task, exclude_user_id=update.effective_user.id,
                message=f"💬 *{name}* добавил комментарий к задаче *#{tid}*: {task['title']}\n\n_{text}_")
        return

    if context.user_data.get("await_tags"):
        tid = context.user_data.pop("await_tags")
        db.update_tags(tid, text)
        t = db.get_task(tid)
        wh.trigger_event("task.updated", update.effective_chat.id, task=t)
        await update.message.reply_text(
            f"🏷 Теги обновлены!\n\n{format_task(t)}", parse_mode="Markdown",
            reply_markup=task_buttons(tid))
        return

    if context.user_data.get("await_assign"):
        tid = context.user_data.pop("await_assign")
        usernames = re.findall(r"@(\w+)", text)
        if usernames:
            for uname in usernames:
                real_id = db.get_user_id_by_username(uname)
                db.add_assignee(tid, real_id or 0, username=uname)
                task = db.get_task(tid)
                # Веб-хук: исполнитель назначен
                wh.trigger_event("assignee.added", update.effective_chat.id, task=task,
                                 extra={"assignee": {"username": uname, "user_id": real_id or 0}})
                # Уведомляем назначенного в личку
                creator_name = update.effective_user.first_name or "Кто-то"
                await notify_single_user(context, user_id=real_id, username=uname,
                    message=f"📩 *{creator_name}* назначил вам задачу *#{tid}*: {task['title']}\n\n"
                            f"{format_task(task)}")
            await update.message.reply_text(
                f"👤 Назначены: {', '.join('@'+u for u in usernames)} на задачу *#{tid}*",
                parse_mode="Markdown")
        else:
            await update.message.reply_text("Не нашёл @username в сообщении.")
        return

    # Обработка как задачи через AI
    processing = await update.message.reply_text("⏳ Анализирую...")
    await _process_task_text(update, context, text, processing)


async def _process_task_text(update, context, text, processing_msg):
    result = extract_task_from_text(claude, text)
    if not result:
        await processing_msg.edit_text("❌ Не удалось обработать. Попробуйте снова.")
        return

    if not result.get("is_task"):
        jarvis_msg = result.get("jarvis_response", "Не обнаружил задачу, сэр.")
        await processing_msg.edit_text(f"🤖 {jarvis_msg}")
        return

    user = update.effective_user
    creator_name = user.first_name or user.username or "?"
    chat_id = update.effective_chat.id

    # Проверка лимита задач
    can_create, plan = subs.check_task_limit(chat_id)
    if not can_create:
        msg = subs.get_upgrade_limit_message(plan, f"Лимит {plan['max_tasks']} задач достигнут.")
        await processing_msg.edit_text(msg, parse_mode="Markdown")
        return

    # Проверка фичи повторяющихся задач
    if result.get("recurrence"):
        can_recur, _ = subs.check_feature(chat_id, "recurring_tasks")
        if not can_recur:
            result["recurrence"] = None  # убираем повтор для free

    task = db.add_task(
        chat_id=chat_id,
        creator_id=user.id,
        title=result.get("title", text[:100]),
        description=result.get("description", ""),
        priority=result.get("priority", "medium"),
        category=result.get("category", ""),
        deadline=result.get("deadline"),
        creator_name=creator_name,
        recurrence=result.get("recurrence"),
    )

    # Веб-хук: задача создана
    wh.trigger_event("task.created", chat_id, task=task)

    # Назначение @упомянутых
    mentioned = result.get("mentioned_usernames", [])
    for uname in mentioned:
        real_id = db.get_user_id_by_username(uname)
        db.add_assignee(task["id"], real_id or 0, username=uname)
        # Веб-хук: исполнитель назначен
        wh.trigger_event("assignee.added", chat_id, task=task,
                         extra={"assignee": {"username": uname, "user_id": real_id or 0}})
        # Уведомляем назначенного лично
        await notify_single_user(context, user_id=real_id, username=uname,
            message=f"📩 *{creator_name}* назначил вам задачу *#{task['id']}*: {task['title']}\n\n"
                    f"{format_task(task)}")

    jarvis_msg = result.get("jarvis_response", "Задача создана, сэр.")
    response = f"🤖 {jarvis_msg}\n\n{format_task(task)}"
    await processing_msg.edit_text(response, parse_mode="Markdown", reply_markup=task_buttons(task["id"]))


# ═══════════════════════════════════════════════════════════════
#  ФАЙЛЫ И ФОТО
# ═══════════════════════════════════════════════════════════════

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user_from_update(update)
    if not await check_access(update): return
    tid = context.user_data.get("await_file")
    msg = update.message

    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
        file_name = "photo.jpg"
    elif msg.document:
        file_id = msg.document.file_id
        file_type = "document"
        file_name = msg.document.file_name or "file"
    else:
        return

    if tid:
        context.user_data.pop("await_file", None)
        db.add_file(tid, file_id, file_type, file_name)
        t = db.get_task(tid)
        await msg.reply_text(
            f"📎 Файл прикреплён к задаче *#{tid}*\n\n{format_task(t)}",
            parse_mode="Markdown", reply_markup=task_buttons(tid))
    elif msg.caption:
        processing = await msg.reply_text("⏳ Создаю задачу из фото...")
        result = extract_task_from_text(claude, msg.caption)
        if result and result.get("is_task"):
            user = update.effective_user
            task = db.add_task(
                chat_id=msg.chat_id, creator_id=user.id,
                title=result.get("title", msg.caption[:100]),
                description=result.get("description", ""),
                priority=result.get("priority", "medium"),
                category=result.get("category", ""),
                deadline=result.get("deadline"),
                creator_name=user.first_name or "?")
            db.add_file(task["id"], file_id, file_type, file_name)
            await processing.edit_text(
                f"📎 Задача с файлом создана!\n\n{format_task(task)}",
                parse_mode="Markdown", reply_markup=task_buttons(task["id"]))
        else:
            await processing.edit_text("Не удалось создать задачу из подписи.")


# ═══════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ И ДАЙДЖЕСТ
# ═══════════════════════════════════════════════════════════════

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Проверка дедлайнов каждые 60 сек — уведомления создателю + исполнителям лично."""
    reminders = db.get_tasks_needing_reminder()

    for task in reminders.get("upcoming", []):
        try:
            dl = datetime.fromisoformat(task["deadline"])
            dl_str = dl.strftime("%d.%m.%Y %H:%M")
            text = (
                f"⏰ *Напоминание!*\n\n"
                f"Задача *#{task['id']}* — {task['title']}\n"
                f"Дедлайн через 15 минут: {dl_str}\n\n"
                f"_Рекомендую приступить немедленно._"
            )
            # Отправляем в чат группы
            try:
                await context.bot.send_message(chat_id=task["chat_id"], text=text, parse_mode="Markdown")
            except Exception:
                pass
            # Отправляем каждому причастному лично
            user_ids = db.get_all_notifiable_users_for_task(task["id"])
            for uid in user_ids:
                try:
                    if uid != task["chat_id"]:  # не дублируем если личный чат
                        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"⚠️ Напоминание user_id={uid}: {e}")
            db.mark_reminded(task["id"], "15min")
            logger.info(f"⏰ Напоминание 15мин: задача #{task['id']}")
        except Exception as e:
            logger.error(f"Ошибка напоминания: {e}")

    for task in reminders.get("overdue", []):
        try:
            text = (
                f"🚨 *Задача просрочена!*\n\n"
                f"Задача *#{task['id']}* — {task['title']}\n"
                f"*ДЕДЛАЙН ПРОШЁЛ!*\n\n"
                f"_Выполните (/done {task['id']}) или обновите дедлайн._"
            )
            # Отправляем в чат группы
            try:
                await context.bot.send_message(chat_id=task["chat_id"], text=text, parse_mode="Markdown")
            except Exception:
                pass
            # Отправляем каждому причастному лично
            user_ids = db.get_all_notifiable_users_for_task(task["id"])
            for uid in user_ids:
                try:
                    if uid != task["chat_id"]:
                        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"⚠️ Напоминание user_id={uid}: {e}")
            db.mark_reminded(task["id"], "overdue")
            logger.info(f"🚨 Просрочка: задача #{task['id']}")
        except Exception as e:
            logger.error(f"Ошибка напоминания: {e}")


# ═══════════════════════════════════════════════════════════════
#  ПОДПИСКИ И ОПЛАТА
# ═══════════════════════════════════════════════════════════════

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий тариф."""
    track_user_from_update(update)
    if not await check_access(update):
        return
    chat_id = update.effective_chat.id
    text = subs.format_plan_info(chat_id)
    text += "\n\nИзменить тариф: /upgrade"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает тарифы и кнопки оплаты."""
    track_user_from_update(update)
    if not await check_access(update):
        return
    text = subs.format_plans_comparison()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Basic — 250 Stars", callback_data="buy_basic")],
        [InlineKeyboardButton("💎 Business — 500 Stars", callback_data="buy_business")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопки покупки."""
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "buy_basic":
        plan_id = "basic"
        plan = subs.PLANS["basic"]
    elif d == "buy_business":
        plan_id = "business"
        plan = subs.PLANS["business"]
    else:
        return

    # Отправляем invoice через Telegram Stars
    try:
        await context.bot.send_invoice(
            chat_id=q.message.chat_id,
            title=f"J.A.R.V.I.S. {plan['name']}",
            description=f"Подписка {plan['name']} на 30 дней. "
                        f"До {plan['max_users']} пользователей.",
            payload=f"sub_{plan_id}_{q.message.chat_id}",
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice(label=plan["name"], amount=plan["price_stars"])],
        )
    except Exception as e:
        logger.error(f"Ошибка создания invoice: {e}")
        # Если Stars не поддерживается, предлагаем ручную активацию
        await q.message.reply_text(
            f"⚠️ Автооплата недоступна.\n\n"
            f"Для активации тарифа *{plan['name']}* "
            f"свяжитесь с администратором.\n\n"
            f"Админ может активировать вручную:\n"
            f"`/activate {plan_id}`",
            parse_mode="Markdown")


async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение оплаты (обязательный шаг для Telegram Stars)."""
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка успешной оплаты."""
    payment = update.message.successful_payment
    payload = payment.invoice_payload  # "sub_basic_123456789"
    parts = payload.split("_")

    if len(parts) >= 2 and parts[0] == "sub":
        plan_id = parts[1]
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        amount = payment.total_amount

        subs.activate_plan(
            chat_id=chat_id,
            plan_name=plan_id,
            user_id=user_id,
            months=1,
            payment_id=payment.telegram_payment_charge_id or "",
            amount_stars=amount
        )

        plan = subs.PLANS.get(plan_id, {})
        await update.message.reply_text(
            f"🎉 *Оплата прошла успешно!*\n\n"
            f"{plan.get('emoji', '⭐')} Тариф *{plan.get('name', plan_id)}* активирован на 30 дней!\n\n"
            f"Спасибо за поддержку, сэр! 🤖",
            parse_mode="Markdown")
        logger.info(f"💰 Оплата: chat={chat_id}, plan={plan_id}, stars={amount}")


async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная активация тарифа (только для админов)."""
    track_user_from_update(update)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Только для администраторов.")
        return
    if not context.args:
        await update.message.reply_text(
            "Укажите тариф: /activate basic или /activate business\n"
            "Для конкретного чата: /activate basic -123456789")
        return

    plan_id = context.args[0].lower()
    if plan_id not in ("basic", "business", "free"):
        await update.message.reply_text("Тарифы: free, basic, business")
        return

    # Можно указать chat_id вторым аргументом
    if len(context.args) > 1:
        try:
            chat_id = int(context.args[1])
        except ValueError:
            chat_id = update.effective_chat.id
    else:
        chat_id = update.effective_chat.id

    if plan_id == "free":
        subs.cancel_plan(chat_id)
        await update.message.reply_text(
            f"🆓 Чат `{chat_id}` переведён на Free.", parse_mode="Markdown")
    else:
        subs.activate_plan(chat_id, plan_id, update.effective_user.id, months=1)
        plan = subs.PLANS[plan_id]
        await update.message.reply_text(
            f"{plan['emoji']} Тариф *{plan['name']}* активирован для чата `{chat_id}` на 30 дней!",
            parse_mode="Markdown")


async def morning_digest(context: ContextTypes.DEFAULT_TYPE):
    """Утренний дайджест в 09:00 UTC+5 (04:00 UTC)."""
    chats = db.get_all_chats_with_tasks()
    for chat_id in chats:
        try:
            tasks = db.get_active_tasks(chat_id)
            if not tasks:
                continue
            overdue = [t for t in tasks if t.get("deadline") and
                       datetime.fromisoformat(t["deadline"]) < datetime.now()]
            today = [t for t in tasks if t.get("deadline") and
                     datetime.fromisoformat(t["deadline"]).date() == datetime.now().date()]
            high = [t for t in tasks if t.get("priority") == "high"]

            lines = ["🌅 *Доброе утро! Утренний дайджест:*\n"]
            lines.append(f"📋 Активных задач: {len(tasks)}")
            if overdue:
                lines.append(f"⚠️ Просроченных: {len(overdue)}")
            if today:
                lines.append(f"\n📅 *На сегодня ({len(today)}):*")
                for t in today:
                    dl = datetime.fromisoformat(t["deadline"]).strftime("%H:%M")
                    lines.append(f"  {PRIORITY_EMOJI.get(t['priority'],'🔵')} {t['title']} — {dl}")
            if high and not today:
                lines.append(f"\n🔴 *Критичные ({len(high)}):*")
                for t in high[:5]:
                    lines.append(f"  {t['title']}")
            lines.append("\n_Хорошего дня!_")

            await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка дайджеста для {chat_id}: {e}")


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════

def main():
    logger.info("🤖 J.A.R.V.I.S. v5.1 инициализация...")

    db.init_db()
    subs.init_subscription_tables()
    logger.info("✅ База данных готова")

    # Запуск веб-сервера в отдельном потоке
    try:
        from web import start_web
        web_thread = threading.Thread(target=start_web, daemon=True)
        web_thread.start()
        logger.info("✅ Веб-дашборд запущен")
    except Exception as e:
        logger.warning(f"⚠️ Веб-дашборд не запустился: {e}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("newproject", cmd_newproject))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))

    # Админ-команды
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("addchat", cmd_addchat))
    app.add_handler(CommandHandler("removechat", cmd_removechat))

    # Подписки
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))

    # Кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Голосовые
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # Файлы и фото
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Напоминания каждые 60 сек
    app.job_queue.run_repeating(check_reminders, interval=60, first=10, name="reminders")

    # Утренний дайджест в 04:00 UTC (= 09:00 Ташкент UTC+5)
    from datetime import time as dt_time
    app.job_queue.run_daily(morning_digest, time=dt_time(hour=4, minute=0), name="digest")

    logger.info("🚀 J.A.R.V.I.S. v5.1 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
