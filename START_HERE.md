# Synapse v1 — с чего начать

Это короткая точка входа для запуска проекта на новом ПК.

Полная инструкция развёртывания лежит в:

```text
DEPLOYMENT.md
```

Ежедневная шпаргалка команд и флагов:

```text
PROJECT_RUNBOOK.md
```

## Что это

Synapse v1 prototype — локальный монолит:

- CRM на FastAPI + PostgreSQL;
- Telegram-панель управления;
- мониторинг сайта вторичного рынка через уже открытый браузерный профиль;
- опциональный LLM-анализ и расчёт примерной экономики.

## Минимальный порядок запуска на новом ПК

1. Установить:
   - Git;
   - Python 3.11+;
   - PostgreSQL 15+ / 17;
   - AdsPower или аналогичный браузерный профиль-менеджер;
   - Telegram bot token от BotFather.

2. Распаковать архив в папку, например:

```text
C:\Users\<USER>\Desktop\Monitor
```

3. Создать окружение и поставить зависимости:

```powershell
cd C:\Users\<USER>\Desktop\Monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

4. Создать локальные конфиги:

```powershell
Copy-Item .\.env.example .\.env
Copy-Item .\monitor_local_settings.example.json .\monitor_local_settings.json
```

5. Заполнить `.env`:

```env
AVITO_TG_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
AVITO_TG_CHAT_ID=YOUR_CHAT_ID
DB_PASSWORD=YOUR_POSTGRES_PASSWORD
```

6. Инициализировать CRM-базу:

```powershell
cd .\crm
..\.venv\Scripts\python.exe -m app.cli init-db
..\.venv\Scripts\python.exe -m app.cli seed
cd ..
```

7. Запустить стек:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

8. Открыть CRM:

```text
http://127.0.0.1:8001/market
```

9. Открыть Telegram-бота, нажать Start. Если клавиатура не пришла:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1 -RestartTelegram
```

10. Открыть браузерный профиль руками, потом запускать поиск кнопками в Telegram.

## Важно

В архиве нет:

- настоящего `.env`;
- Telegram/LLM/API токенов;
- реальных прокси и rotation URL;
- PostgreSQL/SQLite баз;
- логов;
- полной истории объявлений.

Эти данные каждый настраивает локально.
