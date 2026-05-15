"""
J.A.R.V.I.S. — Webhook Dispatcher
Отправляет события задач во внешние сервисы (Zapier, Make.com, custom URLs).
"""

import json
import threading
import logging
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import database as db

logger = logging.getLogger("jarvis.webhooks")


def serialize_task(task):
    """Готовит задачу к отправке в веб-хук."""
    if not task:
        return {}
    chat_id = task.get("chat_id", 0)
    settings = db.get_chat_settings(chat_id) if chat_id else {"key_prefix": "JV"}
    return {
        "id": task.get("id"),
        "key": f"{settings['key_prefix']}-{task.get('id')}",
        "title": task.get("title"),
        "description": task.get("description", ""),
        "status": task.get("status"),
        "priority": task.get("priority"),
        "category": task.get("category", ""),
        "tags": task.get("tags", ""),
        "deadline": task.get("deadline"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
        "archived_at": task.get("archived_at"),
        "kanban_column": task.get("kanban_column", "todo"),
        "task_type": task.get("task_type", "task"),
        "creator_id": task.get("creator_id"),
        "creator_name": task.get("creator_name", ""),
        "chat_id": chat_id,
        "assignees": task.get("assignees", []),
        "subtasks": task.get("subtasks", []),
        "comment_count": task.get("comment_count", 0),
    }


def trigger_event(event, chat_id, task=None, extra=None):
    """Срабатывает на событие. Отправляет всем подписанным веб-хукам в фоне."""
    if not chat_id:
        return
    webhooks = db.get_active_webhooks_for_event(chat_id, event)
    if not webhooks:
        return

    payload = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        "chat_id": chat_id,
    }
    if task:
        payload["task"] = serialize_task(task)
    if extra:
        payload.update(extra)

    # Отправляем в фоне чтобы не блокировать основной поток
    for wh in webhooks:
        thread = threading.Thread(
            target=_send_webhook,
            args=(wh, payload),
            daemon=True
        )
        thread.start()


def _send_webhook(webhook, payload):
    """Отправляет POST-запрос на URL веб-хука."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            webhook["url"],
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "JARVIS-Webhook/1.0",
                "X-JARVIS-Event": payload.get("event", ""),
                "X-JARVIS-Webhook-Id": str(webhook["id"]),
            }
        )
        with urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                db.log_webhook_trigger(webhook["id"], success=True)
                logger.info(f"✅ Webhook #{webhook['id']} → {payload.get('event')} ({resp.status})")
            else:
                db.log_webhook_trigger(
                    webhook["id"], success=False,
                    error=f"HTTP {resp.status}")
                logger.warning(f"⚠️ Webhook #{webhook['id']} returned {resp.status}")
    except HTTPError as e:
        db.log_webhook_trigger(webhook["id"], success=False, error=f"HTTP {e.code}: {e.reason}")
        logger.error(f"❌ Webhook #{webhook['id']} HTTP error: {e.code}")
    except URLError as e:
        db.log_webhook_trigger(webhook["id"], success=False, error=str(e.reason))
        logger.error(f"❌ Webhook #{webhook['id']} URL error: {e.reason}")
    except Exception as e:
        db.log_webhook_trigger(webhook["id"], success=False, error=str(e)[:500])
        logger.error(f"❌ Webhook #{webhook['id']} error: {e}")


def test_webhook(url):
    """Отправляет тестовое сообщение на URL чтобы проверить настройку."""
    try:
        payload = {
            "event": "webhook.test",
            "timestamp": datetime.now().isoformat(),
            "message": "Тестовое уведомление от J.A.R.V.I.S.",
            "task": {
                "id": 0,
                "key": "JV-0",
                "title": "Тестовая задача",
                "priority": "medium",
                "status": "active"
            }
        }
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "JARVIS-Webhook/1.0",
                "X-JARVIS-Event": "webhook.test"
            }
        )
        with urlopen(req, timeout=10) as resp:
            return {"success": 200 <= resp.status < 300, "status": resp.status}
    except HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
    except URLError as e:
        return {"success": False, "error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}
