# Шпаргалка запуска Avito Monitor + CRM

Если нужно развернуть проект на новом ПК с нуля, сначала смотри отдельный файл:

```text
DEPLOYMENT.md
```

Этот runbook — именно ежедневная шпаргалка запуска уже настроенной локальной машины.

Рабочая схема сейчас такая:

1. PostgreSQL хранит основную CRM-базу.
2. CRM/FastAPI работает на `http://127.0.0.1:8001`.
3. Telegram-панель управления запускает/останавливает монитор и показывает статус.
4. Сам Avito-монитор запускается через кнопку в Telegram и подключается к уже открытому AdsPower-профилю.
5. В монорепе CRM лежит в папке `crm/`; старый путь `C:\crm_inventory` остаётся только fallback для локальной машины.

## Самый простой запуск

Из папки `Monitor`:

```powershell
.\start_all.ps1
```

Эта команда сама:

- проверит PostgreSQL на порту `5432`;
- запустит PostgreSQL, если он не поднят;
- проверит CRM на `http://127.0.0.1:8001`;
- запустит CRM из `.\crm`, если папка есть, иначе из `C:\crm_inventory`;
- запустит Telegram-панель управления, если она не поднята.

Сам поиск Авито она не запускает. Для поиска сначала руками открой нужный AdsPower-профиль, потом нажми кнопку в Telegram.

Если PowerShell ругнётся на запуск скриптов, используй такой вариант:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

После успешного запуска порядок такой:

1. Открыть AdsPower.
2. Запустить нужный профиль с прокси.
3. Открыть Telegram-чат с ботом.
4. Если ссылка ещё не задана — нажать `📌 Установить ссылку` и отправить ссылку Авито.
5. Нажать `▶️ Начать поиск`.
6. Подтвердить запуск кнопкой `✅ Да, начать`.

Проверить, что всё живо, можно кнопкой `📊 Статус` в Telegram.

## Запуск из VS Code

В VS Code:

1. Открой папку `C:\Users\Кирилл\Desktop\Monitor`.
2. Нажми `Terminal → Run Task...`.
3. Выбери `Start Avito Monitor Stack`.

Эта задача запускает тот же `start_all.ps1`.

Если хочешь совсем быстро с клавиатуры:

1. Нажми `Ctrl+Shift+P`.
2. Напиши `Run Task`.
3. Выбери `Tasks: Run Task`.
4. Выбери `Start Avito Monitor Stack`.

Если задача пропала, проверь файл:

```text
C:\Users\Кирилл\Desktop\Monitor\.vscode\tasks.json
```

## Быстрый порядок запуска

### 1. Запустить PostgreSQL

Сейчас PostgreSQL установлен здесь:

```powershell
C:\pg17\pgsql\bin\postgres.exe -D C:\pg17\data
```

Проверить, что Postgres жив:

```powershell
Get-NetTCPConnection -LocalPort 5432 -State Listen
```

Если порт `5432` слушается — база поднята.

### 2. Запустить CRM

```powershell
cd .\crm
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Проверить CRM:

```powershell
Invoke-WebRequest http://127.0.0.1:8001/api/market/imports/avito/summary
```

Страницы:

- http://127.0.0.1:8001/bought
- http://127.0.0.1:8001/sold
- http://127.0.0.1:8001/stats
- http://127.0.0.1:8001/history
- http://127.0.0.1:8001/market

### Экспорт/импорт CRM без croc

Сейчас обмен данными не использует `croc` и не отправляет файл на чужой IP.

В верхней панели CRM есть кнопки:

- `Экспорт` — собирает один JSON-файл и сразу скачивает его через браузер;
- `Импорт` — загружает такой JSON обратно в CRM.

В экспорт попадает:

- все мои товары из `Купил` и `Продано`, у которых ещё нет отметки `is_synced`;
- все объявления бота из `market_listings`, у которых ещё нет `exported_at`.

После успешного скачивания CRM помечает строки как отправленные:

- личные товары: `items.is_synced = true`;
- объявления бота: `market_listings.exported_at = время экспорта`.

В следующий экспорт эти строки уже не попадут, чтобы не гонять одно и то же.

### 3. Запустить Telegram-панель

Из папки `Monitor`:

```powershell
cd C:\Users\Кирилл\Desktop\Monitor
.\start_telegram_control_bot.ps1
```

Или напрямую:

```powershell
python -u telegram_control_bot.py --announce
```

После этого в Telegram должны быть кнопки:

- 📌 Установить ссылку
- ▶️ Начать поиск
- ⏹ Закончить поиск
- 📊 Статус
- 🔁 Сменить IP
- 📄 Логи
- 🟢 CRM

### 4. Запустить AdsPower-профиль

Профиль нужно открыть руками в AdsPower. В нём уже должны быть настроены:

- прокси;
- cookies/session;
- fingerprint.

Монитор не должен запускать голый Playwright-браузер.

### 5. Запустить поиск

В Telegram:

1. Нажать `📌 Установить ссылку`.
2. Отправить ссылку Авито.
3. Нажать `▶️ Начать поиск`.
4. Подтвердить запуск.

Монитор будет:

- смотреть выдачу;
- открывать новые объявления;
- сохранять JSON/HTML-бэкап;
- сохранять объявления в PostgreSQL CRM;
- отправлять объявление и анализ в Telegram;
- использовать CRM для оценки профита.

## Проверка процессов

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

Проверить, кто реально слушает CRM-порт:

```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen
```

Проверить, работает ли монитор:

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*qa_automation.py*' } |
  Select-Object ProcessId,CommandLine
```

## Логи и статус

Статус монитора:

```powershell
Get-Content .\json_responses\monitor_status.json
```

Лог Telegram-панели:

```powershell
Get-Content .\telegram_control_bot.out.log -Tail 80
Get-Content .\telegram_control_bot.err.log -Tail 80
```

Лог CRM:

```powershell
Get-Content .\crm\crm_runtime.out.log -Tail 80
Get-Content .\crm\crm_runtime.err.log -Tail 80
```

## Как остановить

Монитор лучше останавливать кнопкой в Telegram:

```text
⏹ Закончить поиск
```

Если нужно руками:

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*qa_automation.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

Telegram-панель:

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*telegram_control_bot.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

CRM:

```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess }
```

PostgreSQL лучше не гасить без причины: это основная база.

## Важные правила

- Не использовать старый IP ноутбука для CRM.
- Рабочий CRM API: `http://127.0.0.1:8001`.
- Основная база теперь PostgreSQL `crm_inventory`.
- Не смешивать старые SQLite/JSON-файлы с текущей рабочей базой вручную.
- AdsPower-прокси настраивается в самом AdsPower-профиле, не в Playwright.
- Файлы `json_responses` — это бэкап/диагностика, источник истины должен быть PostgreSQL.
- Реальные ссылки ротации прокси хранятся только локально: `monitor_local_settings.json` или env `AVITO_PROXY_ROTATION_URLS`.
- В git нельзя отправлять `.env`, `*.log`, `*.db`, `*.dump`, `*.zip`, `.venv`, `json_responses`.
