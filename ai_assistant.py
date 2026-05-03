"""
J.A.R.V.I.S. v5 — AI Assistant
Извлечение задач из текста через Claude API.
"""

import json
import anthropic
from datetime import datetime


def extract_task_from_text(client, text):
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    current_weekday = now.strftime("%A")

    weekdays_ru = {
        "Monday": "Понедельник", "Tuesday": "Вторник",
        "Wednesday": "Среда", "Thursday": "Четверг",
        "Friday": "Пятница", "Saturday": "Суббота",
        "Sunday": "Воскресенье"
    }
    weekday_ru = weekdays_ru.get(current_weekday, current_weekday)

    prompt = f"""Ты — ИИ-ассистент J.A.R.V.I.S. Проанализируй сообщение и извлеки задачу.

Текущая дата: {current_date} ({weekday_ru})
Текущее время: {current_time}

Сообщение:
\"{text}\"

Ответь ТОЛЬКО валидным JSON:
{{
    "is_task": true/false,
    "title": "краткое название (до 100 символов)",
    "description": "детали если есть",
    "priority": "high/medium/low",
    "category": "работа/личное/здоровье/учёба/покупки/другое",
    "deadline": "YYYY-MM-DDTHH:MM:SS или null",
    "recurrence": "daily/weekly/monthly или null",
    "mentioned_usernames": ["username1", "username2"] или [],
    "jarvis_response": "ответ в стиле J.A.R.V.I.S. (1-2 предложения, обращайся 'сэр')"
}}

Правила:
- is_task: false если просто болтовня или вопрос
- «завтра» = следующий день, «через час» = +1 час
- «вечером» = 19:00, «утром» = 09:00, «днём» = 14:00
- «в пятницу» = ближайшая пятница
- «срочно/важно/критично» = high
- «каждый день» = recurrence: "daily"
- «каждую неделю/еженедельно» = recurrence: "weekly"
- «каждый месяц/ежемесячно» = recurrence: "monthly"
- mentioned_usernames: извлеки @username из текста (без @)
- Если нет @упоминаний, оставь пустой массив"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        return json.loads(raw)

    except (json.JSONDecodeError, IndexError, KeyError) as e:
        print(f"[AI] Ошибка парсинга: {e}")
        return None
    except anthropic.APIError as e:
        print(f"[AI] Ошибка API: {e}")
        return None


def generate_status_report(client, tasks):
    if not tasks:
        return "Сэр, список задач пуст. Могу предложить вам отдохнуть? ☕"

    summary = json.dumps(tasks, ensure_ascii=False, indent=2, default=str)

    prompt = f"""Ты — J.A.R.V.I.S. Дай краткий отчёт о задачах (2-3 предложения).
Обращайся "сэр". Упомяни просроченные и ближайшие дедлайны.

Задачи:
{summary}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return "Сэр, отчёт временно недоступен."
