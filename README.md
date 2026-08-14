# Synapse

Synapse is a local-first v1 prototype for tracking second-hand marketplace listings, analyzing item bundles, estimating resale economics, and managing a small inventory CRM.

The project is currently built as a monorepo:

- `crm/` — FastAPI + PostgreSQL CRM with inventory, market data, catalogue, price observations, import/export, and a liquid-glass UI.
- `qa_automation.py` — browser-driven marketplace monitor template.
- `telegram_control_bot.py` — Telegram control panel for starting/stopping the monitor and receiving listing notifications.
- `app/` — extraction, LLM analysis, CRM API integration, notification formatting, and local pipeline helpers.
- `examples/` — small sanitized examples of extracted listing payloads.

## Prototype status

This repository is a **v1 prototype**. The CRM can be tested locally in a normal way, but the marketplace monitor is provided as a template: production use requires your own browser profile, Telegram bot, LLM key, proxy setup, and local configuration.

No private proxy links, API keys, Telegram tokens, production `.env`, local database dumps, or full historical listing exports are included.

## Why AdsPower is mentioned

The monitor is designed to connect to an already opened anti-detect/browser profile through Chrome DevTools Protocol. AdsPower is used as one possible profile manager because it can keep cookies, proxy settings, and browser fingerprint settings outside the code.

In this repository, those settings are intentionally not bundled. Configure them locally if you want to test the monitor.

## Quick start

Install dependencies:

```powershell
pip install -r requirements.txt
```

For a clean install on another PC, use the full deployment guide:

```text
DEPLOYMENT.md
```

Create local configuration files:

```powershell
Copy-Item .\.env.example .\.env
Copy-Item .\monitor_local_settings.example.json .\monitor_local_settings.json
```

Fill only your own local values in `.env` and `monitor_local_settings.json`.

Local `.env` path:

```text
C:\Users\Кирилл\Desktop\Monitor\.env
```

Start the local stack:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

The script starts PostgreSQL/CRM if needed and starts the Telegram control panel. It does not open or configure your browser profile for you: open the prepared profile manually, then use Telegram buttons to set a marketplace link and start/stop monitoring.

CRM opens at:

```text
http://127.0.0.1:8001
```

Useful pages:

- `http://127.0.0.1:8001/bought`
- `http://127.0.0.1:8001/sold`
- `http://127.0.0.1:8001/stats`
- `http://127.0.0.1:8001/history`
- `http://127.0.0.1:8001/market`

## Project map: where to edit what

Most day-to-day changes are concentrated in a few files:

| Task | File |
| --- | --- |
| Start the whole local stack | `start_all.ps1` |
| Browser/marketplace monitoring loop | `qa_automation.py` |
| Local monitor settings, CDP URL, rotation URLs | `monitor_local_settings.json` |
| Telegram control keyboard, allowed users, start/stop/status buttons | `telegram_control_bot.py` |
| Telegram listing/analysis message formatting and photo sending | `app/notifier.py` |
| End-to-end listing pipeline: save, import to CRM, LLM, evaluation, Telegram | `app/pipeline.py` |
| LLM prompt, extraction rules, light/heavy model routing | `app/llm_analyzer.py` |
| CRM API client from monitor to CRM | `app/crm_market.py` |
| Environment loader | `app/env.py` |
| CRM routes/pages/API | `crm/app/main.py` |
| CRM database models | `crm/app/models.py` |
| CRM listing evaluator and profit rules | `crm/app/avito_evaluator.py` |
| Price observations and auto-price helpers | `crm/app/price_observations.py` |
| CRM liquid-glass styles | `crm/static/liquid.css` |
| CRM templates/pages | `crm/templates/` |
| CRM logo shown in the top panel | `crm/static/synapse-logo.png` |
| Regression tests | `tests/` and `crm/tests/` |

Runtime files are mostly written to `json_responses/`. They are local-only and ignored by Git.

## Telegram chats and keyboard access

The same Telegram bot is used for two things:

- sending new listing notifications and analysis messages;
- showing the control keyboard: set link, start search, stop search, status, logs, CRM, analysis toggle.

To add one more chat/user, edit your local `.env`:

```env
AVITO_TG_BOT_TOKEN=your_bot_token
AVITO_TG_CHAT_ID=your_main_chat_id
AVITO_TG_CHAT_IDS=first_extra_chat_id,second_extra_chat_id
AVITO_TG_ARTEM_CHAT_ID=660501420
```

Rules:

- `AVITO_TG_CHAT_ID` is the main chat.
- `AVITO_TG_CHAT_IDS` is a comma-separated list for additional chats.
- `AVITO_TG_ARTEM_CHAT_ID` / `ARTEM_TG_CHAT_ID` are optional aliases for one extra trusted user.
- The control panel grants keyboard access if either the Telegram `chat.id` or `from.id` is present in those variables.
- Listing notifications are sent to every configured chat ID without duplicates.
- A Telegram bot cannot start a private conversation by itself. Every user must open the bot and press Start or send any message once; only then `--announce` can deliver the keyboard.

After changing `.env`, restart only the Telegram panel or run the common start script again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

`start_all.ps1` starts `telegram_control_bot.py --announce`, so the keyboard should be sent to all allowed chats on startup.

## Marketplace monitor template

The monitor expects:

- an already running browser profile with a CDP endpoint;
- local Telegram bot credentials;
- local LLM/API credentials if analysis is enabled;
- optional local proxy rotation URLs.

Real rotation URLs should be stored only in `monitor_local_settings.json` or environment variables. The repository contains only `monitor_local_settings.example.json`.

### Analysis toggle

The Telegram panel supports two monitoring modes:

- **with analysis** — listings are saved, imported into CRM, sent through the LLM/item extraction pipeline, evaluated, and followed by a profit summary;
- **without analysis** — listings are still collected, saved/imported, and sent to Telegram, but paid LLM/evaluation calls are skipped.

The same mode is available from the command line:

```powershell
python .\qa_automation.py
python .\qa_automation.py --no-analysis
```

Use the no-analysis mode when you only want to watch fresh listings without spending money on model calls.

By default, the monitor has no fixed time limit and keeps working until you stop it from Telegram or create the local stop flag. If you need a temporary limited run, pass an explicit limit:

```powershell
python .\qa_automation.py --max-runtime-seconds 9000
```

## What is intentionally local

The repository should not contain:

- real Telegram bot tokens or chat IDs;
- private proxy credentials or rotation links;
- production `.env` files;
- PostgreSQL dumps, SQLite databases, or full listing history;
- runtime logs, queues, screenshots, and temporary extracted payloads.

For a public/demo checkout, use the example configuration files and `examples/extracted_listings.sample.json`.

## Tests

```powershell
python -m pytest -q
```

## Notes

The current code still contains some internal names from the original prototype. They are kept in v1 to avoid breaking the working pipeline. Public documentation describes the project generically as a second-hand marketplace monitoring and CRM prototype.
