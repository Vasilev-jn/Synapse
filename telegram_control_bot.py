from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from app.env import get_env


PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_CRM_ROOT = PROJECT_ROOT / "crm"
CRM_ROOT = LOCAL_CRM_ROOT if LOCAL_CRM_ROOT.exists() else Path(r"C:\crm_inventory")
CRM_PYTHON = CRM_ROOT / ".venv" / "Scripts" / "python.exe"
CRM_URL = "http://127.0.0.1:8001"
CRM_SUMMARY_URL = f"{CRM_URL}/api/market/imports/avito/summary"
CONTROL_DIR = PROJECT_ROOT / "json_responses"
CONTROL_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = CONTROL_DIR / "telegram_control_state.json"
STOP_FLAG = CONTROL_DIR / "monitor_stop.flag"
MONITOR_STATUS_FILE = CONTROL_DIR / "monitor_status.json"
MONITOR_OUT_LOG = PROJECT_ROOT / "monitor_telegram.out.log"
MONITOR_ERR_LOG = PROJECT_ROOT / "monitor_telegram.err.log"
CRM_OUT_LOG = PROJECT_ROOT / "crm_telegram.out.log"
CRM_ERR_LOG = PROJECT_ROOT / "crm_telegram.err.log"
DEFAULT_ARTEM_ID = "660501420"
TERMINAL_MONITOR_STATUSES = {"stopped", "finished", "crashed", "failed", "error"}
MONITOR_STARTUP_WAIT_SECONDS = 22


KEYBOARD = {
    "keyboard": [
        [{"text": "📌 Установить ссылку"}, {"text": "▶️ Начать поиск"}],
        [{"text": "⏹ Закончить поиск"}, {"text": "📊 Статус"}],
        [{"text": "🧠 Анализ"}, {"text": "🔁 Сменить IP"}],
        [{"text": "📄 Логи"}, {"text": "🟢 CRM"}, {"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
}


def default_state() -> dict[str, Any]:
    return {
        "offset": 0,
        "pending_link_chats": [],
        "target_url": None,
        "analysis_enabled": True,
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    data.setdefault("offset", 0)
    data.setdefault("pending_link_chats", [])
    data.setdefault("target_url", None)
    data.setdefault("analysis_enabled", True)
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def bot_token() -> str:
    token = get_env("AVITO_TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("AVITO_TG_BOT_TOKEN is not set")
    return token


def authorized_ids() -> set[str]:
    values: list[str] = [DEFAULT_ARTEM_ID]
    for name in ("AVITO_TG_CHAT_ID", "AVITO_TG_CHAT_IDS", "AVITO_TG_ARTEM_CHAT_ID", "ARTEM_TG_CHAT_ID"):
        raw = get_env(name)
        if raw:
            values.extend(re.split(r"[,\s;]+", raw))
    return {value.strip() for value in values if value and value.strip()}


def telegram_request(method: str, payload: dict[str, Any] | None = None, *, timeout: int = 60) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token()}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    loaded = json.loads(body)
    if not loaded.get("ok"):
        raise RuntimeError(f"Telegram API error: {loaded}")
    return loaded


def send_message(chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    telegram_request("sendMessage", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    telegram_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})


def send_start_confirmation(chat_id: str, target_url: str | None, *, analysis_enabled: bool = True) -> None:
    if not target_url:
        send_message(chat_id, "Ссылка ещё не установлена. Нажми «📌 Установить ссылку» и пришли URL Авито.", reply_markup=KEYBOARD)
        return
    mode_text = "с анализом LLM/профита" if analysis_enabled else "без анализа, только сбор объявлений"
    send_message(
        chat_id,
        f"Запустить поиск по последней ссылке?\n\n"
        f"Режим сейчас: <b>{escape(mode_text)}</b>\n\n"
        f"<code>{escape(target_url)}</code>",
        reply_markup={
            "inline_keyboard": [
                [{"text": "▶️ С анализом", "callback_data": "start_monitor_analysis"}],
                [{"text": "⚡ Без анализа", "callback_data": "start_monitor_no_analysis"}],
                [{"text": "↩️ Отмена", "callback_data": "cancel"}],
            ]
        },
    )


def escape(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_updates(offset: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"timeout": 50, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])})
    response = telegram_request(f"getUpdates?{query}", None, timeout=70)
    result = response.get("result")
    return result if isinstance(result, list) else []


def process_ids_matching(pattern: str) -> list[int]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        output = subprocess.check_output(["powershell", "-NoProfile", "-Command", script], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return []
    return [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]


def monitor_pids() -> list[int]:
    return process_ids_matching("qa_automation.py")


def kill_monitor_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1)
    still_running = [pid for pid in pids if pid in monitor_pids()]
    for pid in still_running:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def monitor_status_value() -> str | None:
    status_data = load_monitor_status()
    if not status_data:
        return None
    value = status_data.get("status")
    return str(value).strip().lower() if value is not None else None


def write_panel_monitor_status(status: str, **extra: Any) -> None:
    payload = {
        "status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    try:
        MONITOR_STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def wait_monitor_startup(process: subprocess.Popen) -> str:
    deadline = time.time() + MONITOR_STARTUP_WAIT_SECONDS
    last_status = "starting"
    while time.time() < deadline:
        status_data = load_monitor_status() or {}
        last_status = str(status_data.get("status") or last_status)
        if last_status == "running":
            pids = monitor_pids()
            return f"Монитор запущен и подключился к AdsPower: PID {', '.join(map(str, pids)) or process.pid}"
        if last_status in {"failed", "error"}:
            return (
                "Монитор стартовал, но упал при запуске.\n"
                f"Причина: {escape(status_data.get('last_error') or status_data.get('reason') or tail_file(MONITOR_ERR_LOG, 1200))}"
            )
        if process.poll() is not None:
            return (
                "Монитор завершился сразу после запуска.\n"
                f"Статус: {escape(human_monitor_status(last_status))}\n"
                f"Последняя ошибка:\n{escape(tail_file(MONITOR_ERR_LOG, 1200))}"
            )
        time.sleep(0.5)

    if process.poll() is None and last_status in {"starting", "connecting_adspower"}:
        try:
            process.terminate()
        except OSError:
            pass
        write_panel_monitor_status(
            status="failed",
            reason="startup_timeout_waiting_for_adspower_cdp",
            last_error="Монитор завис на подключении к AdsPower/CDP. Закройте и заново откройте профиль AdsPower.",
        )
        return (
            "Монитор завис на подключении к AdsPower/CDP и был остановлен.\n"
            "Что сделать: закрой профиль AdsPower, открой его заново и нажми «Начать поиск» ещё раз."
        )

    pids = monitor_pids()
    return f"Монитор запущен: PID {', '.join(map(str, pids)) or process.pid}. Статус: {escape(human_monitor_status(last_status))}"


def crm_pids() -> list[int]:
    return process_ids_matching("uvicorn app.main")


def crm_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(CRM_SUMMARY_URL, timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def start_crm() -> str:
    if crm_is_healthy():
        return "CRM уже работает."
    if not CRM_PYTHON.exists():
        return f"Не нашёл Python CRM: {CRM_PYTHON}"
    CRM_OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = CRM_OUT_LOG.open("a", encoding="utf-8")
    err = CRM_ERR_LOG.open("a", encoding="utf-8")
    subprocess.Popen(
        [str(CRM_PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"],
        cwd=str(CRM_ROOT),
        stdout=out,
        stderr=err,
        creationflags=0x08000000,
    )
    time.sleep(3)
    return "CRM запущена." if crm_is_healthy() else "CRM стартовала, но health-check пока не отвечает. Смотри crm_telegram.err.log."


def start_monitor(target_url: str | None, *, analysis_enabled: bool = True) -> str:
    pids = monitor_pids()
    status_value = monitor_status_value()
    if pids and (STOP_FLAG.exists() or status_value in TERMINAL_MONITOR_STATUSES):
        kill_monitor_pids(pids)
        pids = monitor_pids()
    if pids:
        return f"Монитор уже запущен: PID {', '.join(map(str, pids))}"
    if STOP_FLAG.exists():
        try:
            STOP_FLAG.unlink()
        except OSError:
            pass
    args = [sys.executable, "-u", "qa_automation.py"]
    if target_url:
        args.extend(["--target-url", target_url])
    if not analysis_enabled:
        args.append("--no-analysis")
    MONITOR_OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_panel_monitor_status(status="starting", target_url=target_url, analysis_enabled=analysis_enabled)
    session_header = f"=== monitor session started {datetime.now().isoformat(timespec='seconds')} ===\n"
    try:
        MONITOR_OUT_LOG.write_text(session_header, encoding="utf-8")
        MONITOR_ERR_LOG.write_text(session_header, encoding="utf-8")
    except OSError:
        pass
    out = MONITOR_OUT_LOG.open("a", encoding="utf-8")
    err = MONITOR_ERR_LOG.open("a", encoding="utf-8")
    process = subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        stdout=out,
        stderr=err,
        creationflags=0x08000000,
    )
    mode_text = "с анализом" if analysis_enabled else "без анализа"
    return f"{wait_monitor_startup(process)}\nРежим: {mode_text}."


def stop_monitor() -> str:
    STOP_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    pids = monitor_pids()
    if not pids:
        write_panel_monitor_status(status="stopped", reason="stop_requested_but_process_not_found")
        return "Флаг остановки поставлен. Монитор сейчас не найден."
    write_panel_monitor_status(status="stopping", pids=pids)
    return f"Флаг остановки поставлен. Монитор завершится мягко. PID: {', '.join(map(str, pids))}"


def rotate_ip() -> str:
    try:
        import qa_automation

        qa_automation.rotate_proxy_ip()
        return "IP-ротация выполнена."
    except Exception as error:
        return f"Ошибка ротации: {escape(error)}"


def tail_file(path: Path, limit: int = 2500) -> str:
    if not path.exists():
        return "файл не найден"
    try:
        raw = path.read_bytes()
    except OSError as error:
        return f"ошибка чтения: {error}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    return text[-limit:] or "пусто"


def status_text(state: dict[str, Any]) -> str:
    monitor = monitor_pids()
    crm_ok = crm_is_healthy()
    status_data = load_monitor_status()
    status_lines: list[str] = []
    if status_data:
        raw_status = str(status_data.get("status") or "unknown")
        if not monitor and raw_status in {"starting", "connecting_adspower", "stopping"}:
            raw_status = "stopped"
        status_icon = {
            "ok": "✅",
            "error": "⚠️",
            "stopping": "🟡",
            "starting": "🟦",
            "connecting_adspower": "🔌",
            "stopped": "⏹",
            "finished": "🏁",
            "failed": "❌",
        }.get(raw_status, "ℹ️")
        status_lines.extend(
            [
                f"{status_icon} Состояние: {escape(human_monitor_status(raw_status))}",
                f"🔄 Цикл: {escape(status_data.get('cycle_number', '—'))}",
                f"🆕 Новых за прошлый цикл: {escape(display_value(status_data.get('saved_in_cycle')))}",
                f"👀 Видели всего: {escape(display_value(status_data.get('seen_count')))}",
                f"⛔ Отложено/ошибок: {escape(display_value(status_data.get('failed_count')))}",
            ]
        )
        next_wait = status_data.get("next_run_after_seconds")
        if next_wait is not None and monitor:
            status_lines.append(f"⏱ Следующая проверка примерно через: {escape(display_value(next_wait))} сек")
        if status_data.get("last_error"):
            status_lines.append(f"❗ Последняя ошибка: {escape(status_data.get('last_error'))}")
        if status_data.get("updated_at"):
            status_lines.append(f"🕒 Обновлено: {escape(human_datetime(status_data.get('updated_at')))}")
    else:
        status_lines.append("ℹ️ monitor_status.json пока не найден")

    link = str(state.get("target_url") or "")
    link_text = telegram_link_text(link) if link else "не установлена"
    monitor_text = f"✅ запущен, PID {', '.join(map(str, monitor))}" if monitor else "⏹ не запущен"
    crm_text = "✅ работает" if crm_ok else "❌ не отвечает"
    analysis_enabled = bool((status_data or {}).get("analysis_enabled", state.get("analysis_enabled", True)))
    analysis_text = "✅ включён" if analysis_enabled else "⚡ выключен, только сбор"
    return (
        f"<b>Статус</b>\n"
        f"CRM: {crm_text}\n"
        f"Монитор: {monitor_text}\n"
        f"Анализ: {analysis_text}\n\n"
        f"<b>Поиск</b>\n"
        f"Ссылка:\n{link_text}\n\n"
        f"<b>Последний цикл</b>\n"
        + "\n".join(status_lines)
    )


def load_monitor_status() -> dict[str, Any] | None:
    if MONITOR_STATUS_FILE.exists():
        try:
            data = json.loads(MONITOR_STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def human_monitor_status(status: str) -> str:
    return {
        "ok": "последний цикл без ошибок",
        "error": "последний цикл с ошибкой",
        "stopping": "останавливается",
        "starting": "запускается",
        "connecting_adspower": "подключается к AdsPower",
        "stopped": "остановлен",
        "finished": "завершён по лимиту времени",
        "failed": "упал при запуске",
    }.get(status, status)


def display_value(value: object) -> object:
    return "—" if value is None or value == "" else value


def human_datetime(value: object) -> str:
    if not value:
        return "—"
    text = str(value)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    return parsed.strftime("%d.%m.%Y %H:%M:%S")


def telegram_link_text(url: str) -> str:
    safe_url = escape(url)
    return f'<a href="{safe_url}">{safe_url}</a>'


def file_mtime_text(path: Path) -> str:
    if not path.exists():
        return "нет файла"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")
    except OSError:
        return "не удалось прочитать время"


def logs_text() -> str:
    monitor = monitor_pids()
    status_data = load_monitor_status()
    status = str(status_data.get("status") or "unknown") if status_data else "нет status-файла"
    monitor_line = f"✅ монитор запущен, PID {', '.join(map(str, monitor))}" if monitor else "⏹ монитор не запущен"
    stale_note = "" if monitor else "\n⚠️ Ниже может быть старый лог прошлого запуска."
    return (
        f"<b>Логи монитора</b>\n"
        f"{monitor_line}\n"
        f"Статус: {escape(human_monitor_status(status))}\n"
        f"stdout обновлён: {escape(file_mtime_text(MONITOR_OUT_LOG))}\n"
        f"stderr обновлён: {escape(file_mtime_text(MONITOR_ERR_LOG))}"
        f"{stale_note}\n\n"
        "<b>monitor stdout</b>\n"
        f"<code>{escape(tail_file(MONITOR_OUT_LOG, 2200))}</code>\n\n"
        "<b>monitor stderr</b>\n"
        f"<code>{escape(tail_file(MONITOR_ERR_LOG, 1200))}</code>"
    )


def extract_avito_url(text: str) -> str | None:
    match = re.search(r"https?://(?:www\.)?avito\.ru/\S+", text)
    if not match:
        return None
    return match.group(0).strip("()[]<>.,")


def command_text(text: str) -> str:
    normalized = text.strip().lower()
    normalized = re.sub(r"^[^\wа-яё]+", "", normalized, flags=re.IGNORECASE).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def handle_text(chat_id: str, user_id: str, text: str, state: dict[str, Any]) -> None:
    allowed = authorized_ids()
    if chat_id not in allowed and user_id not in allowed:
        send_message(chat_id, "Нет доступа к управлению этим ботом.")
        return

    pending = set(str(value) for value in state.get("pending_link_chats", []))
    url = extract_avito_url(text)
    if chat_id in pending or user_id in pending:
        if not url:
            send_message(chat_id, "Пришли ссылку Авито целиком, начиная с https://www.avito.ru/...", reply_markup=KEYBOARD)
            return
        state["target_url"] = url
        pending.discard(chat_id)
        pending.discard(user_id)
        state["pending_link_chats"] = sorted(pending)
        save_state(state)
        send_message(
            chat_id,
            f"Ссылка установлена:\n<code>{escape(url)}</code>",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "▶️ С анализом", "callback_data": "start_monitor_analysis"}],
                    [{"text": "⚡ Без анализа", "callback_data": "start_monitor_no_analysis"}],
                ]
            },
        )
        send_message(chat_id, "Клавиатура управления на месте.", reply_markup=KEYBOARD)
        return

    if url and ("установ" not in text.lower()):
        state["target_url"] = url
        save_state(state)
        send_message(chat_id, f"Ссылка сохранена:\n<code>{escape(url)}</code>", reply_markup=KEYBOARD)
        send_start_confirmation(chat_id, url, analysis_enabled=bool(state.get("analysis_enabled", True)))
        return

    normalized = command_text(text)
    if normalized in {"/start", "start", "❓ помощь", "помощь"}:
        send_message(chat_id, "Управление Avito-монитором. Выбирай кнопкой снизу.", reply_markup=KEYBOARD)
    elif "установить" in normalized and "ссыл" in normalized:
        pending.add(chat_id)
        pending.add(user_id)
        state["pending_link_chats"] = sorted(pending)
        save_state(state)
        send_message(chat_id, "Ок, пришли новую ссылку Авито одним сообщением.", reply_markup=KEYBOARD)
    elif (
        "закончить" in normalized
        or "останов" in normalized
        or normalized in {"стоп", "stop", "stop search", "end search"}
    ):
        send_message(chat_id, stop_monitor(), reply_markup=KEYBOARD)
    elif (
        "начать" in normalized
        or normalized in {"поиск", "start search", "start"}
        or normalized.startswith("начать поиск")
    ):
        send_start_confirmation(chat_id, state.get("target_url"), analysis_enabled=bool(state.get("analysis_enabled", True)))
    elif "анализ" in normalized:
        enabled = not bool(state.get("analysis_enabled", True))
        state["analysis_enabled"] = enabled
        save_state(state)
        mode = "включён: будут LLM и расчёт профита" if enabled else "выключен: только сбор объявлений без платного анализа"
        send_message(chat_id, f"🧠 Анализ {mode}.", reply_markup=KEYBOARD)
    elif "статус" in normalized:
        send_message(chat_id, status_text(state), reply_markup=KEYBOARD)
    elif "сменить" in normalized or "ip" in normalized or "айпи" in normalized:
        send_message(chat_id, rotate_ip(), reply_markup=KEYBOARD)
    elif "логи" in normalized:
        send_message(chat_id, logs_text(), reply_markup=KEYBOARD)
    elif "crm" in normalized:
        send_message(chat_id, start_crm(), reply_markup=KEYBOARD)
    else:
        send_message(chat_id, "Не понял команду. Нажми кнопку снизу.", reply_markup=KEYBOARD)


def handle_callback(callback: dict[str, Any], state: dict[str, Any]) -> None:
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
    chat_id = str(chat.get("id") or "")
    user_id = str(user.get("id") or "")
    allowed = authorized_ids()
    if chat_id not in allowed and user_id not in allowed:
        answer_callback(callback_id, "Нет доступа")
        return
    if data == "start_monitor":
        answer_callback(callback_id, "Запускаю")
        send_message(
            chat_id,
            start_monitor(state.get("target_url"), analysis_enabled=bool(state.get("analysis_enabled", True))),
            reply_markup=KEYBOARD,
        )
    elif data == "start_monitor_analysis":
        state["analysis_enabled"] = True
        save_state(state)
        answer_callback(callback_id, "Запускаю с анализом")
        send_message(chat_id, start_monitor(state.get("target_url"), analysis_enabled=True), reply_markup=KEYBOARD)
    elif data == "start_monitor_no_analysis":
        state["analysis_enabled"] = False
        save_state(state)
        answer_callback(callback_id, "Запускаю без анализа")
        send_message(chat_id, start_monitor(state.get("target_url"), analysis_enabled=False), reply_markup=KEYBOARD)
    elif data == "cancel":
        answer_callback(callback_id, "Отмена")
        send_message(chat_id, "Отменил.", reply_markup=KEYBOARD)


def announce_to_allowed(text: str) -> None:
    for chat_id in sorted(authorized_ids()):
        try:
            send_message(chat_id, text, reply_markup=KEYBOARD)
        except Exception:
            continue


def run(*, announce: bool = False) -> None:
    state = load_state()
    if announce:
        announce_to_allowed("Панель управления Avito-монитором запущена.")
    while True:
        try:
            updates = get_updates(int(state.get("offset") or 0))
            for update in updates:
                state["offset"] = max(int(state.get("offset") or 0), int(update.get("update_id") or 0) + 1)
                if isinstance(update.get("message"), dict):
                    message = update["message"]
                    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                    user = message.get("from") if isinstance(message.get("from"), dict) else {}
                    text = str(message.get("text") or "")
                    if text:
                        handle_text(str(chat.get("id") or ""), str(user.get("id") or ""), text, state)
                if isinstance(update.get("callback_query"), dict):
                    handle_callback(update["callback_query"], state)
                save_state(state)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            CONTROL_DIR.joinpath("telegram_control_bot.err.log").open("a", encoding="utf-8").write(
                f"{datetime.now().isoformat(timespec='seconds')} {error}\n"
            )
            time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram control panel for Avito monitor")
    parser.add_argument("--announce", action="store_true", help="Send keyboard to allowed chats on startup")
    args = parser.parse_args()
    run(announce=args.announce)
