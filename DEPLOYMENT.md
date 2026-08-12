# Synapse v1 deployment guide

Эта инструкция — для развёртывания Synapse на новом Windows-ПК с нуля.

Synapse v1 состоит из:

- PostgreSQL — основная база CRM;
- FastAPI CRM — веб-интерфейс и API на `http://127.0.0.1:8001`;
- Telegram-панель — кнопки управления мониторингом;
- marketplace monitor — браузерный монитор, который подключается к уже открытому профилю AdsPower/аналогичного браузерного менеджера через CDP.

В репозитории нет production-секретов: токенов Telegram, приватных прокси, реальных rotation URL, `.env`, дампов БД и полной истории объявлений.

## 1. Что установить на новый ПК

Минимально нужно:

1. Git.
2. Python 3.11+.
3. PostgreSQL 15+ или 17.
4. AdsPower или другой браузерный профиль-менеджер с CDP/debug endpoint.
5. Telegram bot token от BotFather.

Проверка:

```powershell
git --version
python --version
psql --version
```

Если `psql` не находится, добавь `bin` папку PostgreSQL в `PATH` или используй полный путь к `psql.exe`.

## 2. Скачать проект

```powershell
cd C:\Users\<USER>\Desktop
git clone https://github.com/Vasilev-jn/Synapse.git Monitor
cd .\Monitor
```

Если используется приватный репозиторий, сначала войди в GitHub в браузере/Git Credential Manager.

## 3. Создать виртуальное окружение и поставить зависимости

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Playwright-браузер сам по себе не является рабочим профилем мониторинга, но пакет Playwright нужен для CDP-подключения:

```powershell
python -m playwright install chromium
```

## 4. Создать локальные конфиги

```powershell
Copy-Item .\.env.example .\.env
Copy-Item .\monitor_local_settings.example.json .\monitor_local_settings.json
```

Эти файлы локальные и не должны коммититься.

## 5. Настроить `.env`

Файл:

```text
C:\Users\<USER>\Desktop\Monitor\.env
```

Минимальный пример:

```env
# Telegram
AVITO_TG_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
AVITO_TG_CHAT_ID=YOUR_MAIN_CHAT_ID
AVITO_TG_CHAT_IDS=SECOND_CHAT_ID,THIRD_CHAT_ID
AVITO_TG_ARTEM_CHAT_ID=

# LLM, only needed when analysis mode is enabled
POLZA_API_KEY=
OPENROUTER_API_KEY=

# CRM API used by monitor
CRM_MARKET_API_URL=http://127.0.0.1:8001/api/market/imports/avito
CRM_MARKET_EVALUATE_API_URL=http://127.0.0.1:8001/api/market/evaluate-avito-listing
CRM_MARKET_EVALUATE_TIMEOUT_SECONDS=30
CRM_MARKET_API_TOKEN=
MARKET_IMPORT_TOKEN=

# PostgreSQL
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=crm_inventory
DB_USER=postgres
DB_PASSWORD=YOUR_POSTGRES_PASSWORD
```

Telegram:

- `AVITO_TG_CHAT_ID` — основной чат;
- `AVITO_TG_CHAT_IDS` — дополнительные чаты через запятую;
- всем этим чатам будут приходить уведомления;
- эти же ID получают доступ к клавиатуре управления.

Если хочешь включить анализ объявлений, заполни `POLZA_API_KEY` или другой поддерживаемый ключ, который используется в `app/llm_analyzer.py`.

## 6. Настроить `monitor_local_settings.json`

Файл:

```text
C:\Users\<USER>\Desktop\Monitor\monitor_local_settings.json
```

Пример:

```json
{
  "rotation_urls": [
    "https://changeip.mobileproxy.space/?proxy_key=YOUR_KEY",
    "https://aproxy.site/?proxy_key=YOUR_KEY",
    "http://81.200.155.214/?proxy_key=YOUR_KEY"
  ]
}
```

Если ротация не нужна, оставь список пустым:

```json
{
  "rotation_urls": []
}
```

Прокси должны быть настроены в AdsPower/браузерном профиле, а не в Playwright-коде.

## 7. PostgreSQL

Создай пользователя/пароль при установке PostgreSQL. Обычно используется пользователь `postgres`.

Проверить, что PostgreSQL слушает порт:

```powershell
Get-NetTCPConnection -LocalPort 5432 -State Listen
```

Создать базу вручную можно так:

```powershell
psql -U postgres -c "CREATE DATABASE crm_inventory;"
```

Если база уже есть, команда может вернуть ошибку `already exists` — это нормально.

CRM также умеет попытаться создать базу автоматически при старте, если может подключиться к admin DB `postgres`.

## 8. Инициализировать CRM-базу

Из корня проекта:

```powershell
cd .\crm
..\.venv\Scripts\python.exe -m app.cli init-db
..\.venv\Scripts\python.exe -m app.cli seed
cd ..
```

`init-db` создаёт таблицы, `seed` добавляет базовые категории/каталог.

Если у тебя есть дамп PostgreSQL от старой установки, восстанавливай его до `seed`, либо в чистую базу:

```powershell
pg_restore -U postgres -d crm_inventory --clean --if-exists path\to\backup.dump
```

После восстановления можно запустить:

```powershell
cd .\crm
..\.venv\Scripts\python.exe -m app.cli init-db
..\.venv\Scripts\python.exe -m app.cli seed
cd ..
```

## 9. Запустить весь стек

Из корня проекта:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

Скрипт делает три вещи:

1. проверяет PostgreSQL;
2. запускает CRM на `http://127.0.0.1:8001`;
3. запускает Telegram-панель с `--announce`.

После запуска клавиатура должна прилететь во все разрешённые Telegram-чаты.

## 10. Проверить CRM

Открой:

- `http://127.0.0.1:8001/bought`;
- `http://127.0.0.1:8001/sold`;
- `http://127.0.0.1:8001/stats`;
- `http://127.0.0.1:8001/history`;
- `http://127.0.0.1:8001/market`.

Проверить API:

```powershell
Invoke-WebRequest http://127.0.0.1:8001/api/market/imports/avito/summary
```

Ожидаемо: HTTP 200 и JSON-ответ.

## 11. Настроить браузерный профиль

В AdsPower/аналогичном менеджере:

1. создай профиль;
2. настрой proxy/cookies/fingerprint;
3. включи/проверь CDP/debug endpoint;
4. запусти профиль руками;
5. убедись, что CDP endpoint совпадает с тем, что ожидает монитор.

По умолчанию монитор использует стандартную локальную схему подключения. Если на другом ПК CDP отличается, правь `qa_automation.py` или вынеси значение в локальный конфиг.

## 12. Запустить мониторинг через Telegram

В Telegram:

1. нажми `📌 Установить ссылку`;
2. отправь ссылку выдачи сайта вторичного рынка;
3. нажми `🧠 Анализ`, если надо переключить режим;
4. нажми `▶️ Начать поиск`;
5. выбери:
   - `▶️ С анализом`;
   - `⚡ Без анализа`.

Режимы:

- с анализом — LLM + CRM evaluation + Telegram-анализ;
- без анализа — только сбор/сохранение/уведомления, без платных LLM-вызовов.

По умолчанию монитор работает без ограничения по времени и останавливается кнопкой:

```text
⏹ Закончить поиск
```

## 13. Логи и диагностика

Статус монитора:

```powershell
Get-Content .\json_responses\monitor_status.json
```

События монитора:

```powershell
Get-Content .\json_responses\monitor_events.jsonl -Tail 80
```

Telegram-панель:

```powershell
Get-Content .\telegram_control_bot.out.log -Tail 80
Get-Content .\telegram_control_bot.err.log -Tail 80
```

CRM:

```powershell
Get-Content .\crm\crm_runtime.out.log -Tail 80
Get-Content .\crm\crm_runtime.err.log -Tail 80
```

Процессы:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -like '*qa_automation.py*' -or
    $_.CommandLine -like '*telegram_control_bot.py*' -or
    $_.CommandLine -like '*uvicorn app.main*' -or
    $_.Name -like '*postgres*'
  } |
  Select-Object ProcessId,Name,CommandLine
```

## 14. Проверки перед работой

```powershell
python -m py_compile qa_automation.py telegram_control_bot.py app\pipeline.py crm\app\main.py crm\app\avito_evaluator.py
python -m pytest -q tests
```

Для CRM-тестов из папки `crm`:

```powershell
cd .\crm
..\.venv\Scripts\python.exe -m pytest -q tests
cd ..
```

Если полный CRM-набор падает на старых тестах, сначала проверь, не известные ли это legacy-тесты. Для проверки evaluator-части можно запускать targeted-набор из `crm/tests`.

## 15. Что нельзя переносить в Git

Не коммить:

- `.env`;
- `monitor_local_settings.json`;
- `json_responses/`;
- `*.log`;
- `*.db`, `*.sqlite`, `*.dump`;
- `*.zip`;
- `.venv/`;
- реальные rotation URLs;
- Telegram/LLM/API токены.

Проверить перед коммитом:

```powershell
git status --short
rg -n "proxy_key=|BOT_TOKEN|POLZA_API_KEY|OPENROUTER_API_KEY|MARKET_IMPORT_TOKEN|CRM_MARKET_API_TOKEN" .
```

## 16. Минимальный чеклист “готово”

- [ ] PostgreSQL слушает `5432`.
- [ ] CRM открывается на `http://127.0.0.1:8001/market`.
- [ ] `GET /api/market/imports/avito/summary` возвращает 200.
- [ ] Telegram-панель прислала клавиатуру.
- [ ] AdsPower/браузерный профиль открыт руками.
- [ ] В Telegram установлена ссылка.
- [ ] Монитор стартует кнопкой и статус показывает PID.
- [ ] Новое объявление сохраняется в CRM.
- [ ] В режиме анализа приходит отдельное сообщение с оценкой.
- [ ] В режиме без анализа объявления приходят без LLM/профит-сообщения.
