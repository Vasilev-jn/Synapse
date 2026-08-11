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

Create local configuration files:

```powershell
Copy-Item .\.env.example .\.env
Copy-Item .\monitor_local_settings.example.json .\monitor_local_settings.json
```

Fill only your own local values in `.env` and `monitor_local_settings.json`.

Start the local stack:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all.ps1
```

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

## Marketplace monitor template

The monitor expects:

- an already running browser profile with a CDP endpoint;
- local Telegram bot credentials;
- local LLM/API credentials if analysis is enabled;
- optional local proxy rotation URLs.

Real rotation URLs should be stored only in `monitor_local_settings.json` or environment variables. The repository contains only `monitor_local_settings.example.json`.

## Tests

```powershell
python -m pytest -q
```

## Notes

The current code still contains some internal names from the original prototype. They are kept in v1 to avoid breaking the working pipeline. Public documentation describes the project generically as a second-hand marketplace monitoring and CRM prototype.
