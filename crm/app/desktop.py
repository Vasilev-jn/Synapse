"""Desktop launcher and Windows automation for the CRM."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import html
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

import uvicorn

from app.analysis_exchange import build_incremental_analysis_export_file, import_received_json_files, receive_once_via_croc, send_current_export_via_croc
from app.config import CURRENT_EXPORT_PATH, DATA_DIR, RUNTIME_DIR, Settings, ensure_data_dirs, get_settings, load_dotenv
from app.db import get_engine, init_db, make_session_factory, session_scope


APP_NAME = "CRM Inventory"
EXE_TASK_PREFIX = "CRM Inventory"
SERVER_START_ERRORS: list[str] = []


def main() -> None:
    parser = argparse.ArgumentParser(description="CRM desktop launcher")
    parser.add_argument("--install-tasks", action="store_true", help="Install Windows scheduled tasks.")
    parser.add_argument("--sync-build-export", action="store_true", help="Build queue/current_export.json once.")
    parser.add_argument("--sync-send-once", action="store_true", help="Try to send queue/current_export.json once.")
    parser.add_argument("--sync-receive-once", action="store_true", help="Listen for one incoming croc transfer.")
    parser.add_argument("--sync-send-worker", action="store_true", help="Run the croc send worker.")
    parser.add_argument("--sync-receive-worker", action="store_true", help="Run the croc receive worker.")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--no-window", action="store_true", help="Start only the local server.")
    args = parser.parse_args()

    ensure_default_env()
    ensure_data_dirs()
    ensure_runtime_croc()
    settings = get_settings()

    if args.install_tasks:
        install_windows_tasks()
        return
    if args.sync_build_export:
        run_logged("Build export", lambda: build_export_once(settings))
        return
    if args.sync_send_once:
        run_logged("Send once", lambda: send_once(settings, timeout_seconds=args.timeout_seconds))
        return
    if args.sync_receive_once:
        run_logged("Receive once", lambda: receive_once(settings, timeout_seconds=args.timeout_seconds))
        return
    if args.sync_send_worker:
        send_worker(settings, interval_seconds=args.interval_seconds, timeout_seconds=args.timeout_seconds)
        return
    if args.sync_receive_worker:
        receive_worker(settings)
        return

    install_windows_tasks(silent=True)
    launch_desktop_window(show_window=not args.no_window)


def ensure_default_env() -> None:
    env_path = RUNTIME_DIR / ".env"
    my_id = default_my_id()
    defaults = default_env_values(my_id)
    if env_path.exists():
        apply_env_defaults(env_path, defaults)
        load_dotenv(env_path)
        return
    env_path.write_text(format_env(defaults), encoding="utf-8")
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    load_dotenv(env_path)


def default_my_id() -> str:
    default_user = os.getenv("USERNAME", "user").strip().lower() or "user"
    if default_user in {"artem", "tema", "артем", "артём"}:
        return "artem"
    if default_user in {"vasol", "kirill"}:
        return "kirill"
    return default_user


def default_env_values(my_id: str) -> dict[str, str]:
    db_name = f"crm_{my_id}"
    return {
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": db_name,
        "DB_USER": "postgres",
        "DB_PASSWORD": "postgres",
        "DATABASE_URL": postgres_url(db_name=db_name, user="postgres", password="postgres", host="localhost", port="5432"),
        "MY_ID": my_id,
        "TOTAL_USERS": "kirill,artem",
        "CROC_PATH": "tools\\croc.exe",
        "CRM_EXPORT_SOURCE_NAME": my_id,
        "CRM_EXPORT_SOURCE_ID": my_id,
    }


def postgres_url(*, db_name: str, user: str, password: str, host: str, port: str) -> str:
    auth = quote_plus(user)
    if password:
        auth += f":{quote_plus(password)}"
    return f"postgresql+psycopg://{auth}@{host}:{port}/{quote_plus(db_name)}"


def apply_env_defaults(env_path: Path, defaults: dict[str, str]) -> None:
    values: dict[str, str] = {}
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    changed = False
    next_lines: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            next_lines.append(raw_line)
            continue
        key, old_value = stripped.split("=", 1)
        key = key.strip()
        value = old_value.strip().strip('"').strip("'")
        replacement = defaults.get(key)
        should_replace = False
        if replacement is not None and not value:
            should_replace = True
        if key == "DATABASE_URL" and value.startswith("sqlite:///"):
            replacement = defaults["DATABASE_URL"]
            should_replace = True
        if should_replace and replacement is not None:
            next_lines.append(f"{key}={replacement}")
            os.environ[key] = replacement
            changed = True
        else:
            next_lines.append(raw_line)
        seen.add(key)

    for key, value in defaults.items():
        if key not in seen:
            next_lines.append(f"{key}={value}")
            os.environ[key] = value
            changed = True

    if changed:
        env_path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def format_env(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def ensure_runtime_croc() -> None:
    runtime_croc = RUNTIME_DIR / "tools" / "croc.exe"
    if runtime_croc.exists():
        return
    bundled_croc = Path(getattr(sys, "_MEIPASS", RUNTIME_DIR)) / "tools" / "croc.exe"
    if not bundled_croc.exists():
        return
    runtime_croc.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled_croc, runtime_croc)


def session_factory_for(settings: Settings):
    engine = get_engine(settings=settings)
    init_db(engine)
    return make_session_factory(engine)


def build_export_once(settings: Settings) -> None:
    session_factory = session_factory_for(settings)
    with session_scope(session_factory) as session:
        result = build_incremental_analysis_export_file(session, settings)
    if result.path is None:
        log_line("No unsynced rows. Queue is empty.")
    else:
        log_line(f"Queued {result.exported_count} rows: {result.path}")


def run_logged(label: str, func) -> None:
    try:
        func()
    except Exception as exc:
        log_line(f"{label} error: {exc}")


def send_once(settings: Settings, *, timeout_seconds: int = 90) -> None:
    with single_instance_lock("send") as locked:
        if not locked:
            return
        session_factory = session_factory_for(settings)
        with session_scope(session_factory) as session:
            result = send_current_export_via_croc(session, settings, timeout_seconds=timeout_seconds)
        log_send_result(result, prefix="Send once")


def send_worker(settings: Settings, *, interval_seconds: int = 300, timeout_seconds: int = 90) -> None:
    interval_seconds = max(10, interval_seconds)
    session_factory = session_factory_for(settings)
    while True:
        with single_instance_lock("send") as locked:
            if not locked:
                time.sleep(interval_seconds)
                continue
            with session_scope(session_factory) as session:
                result = send_current_export_via_croc(session, settings, timeout_seconds=timeout_seconds)
            log_send_result(result, prefix="Send worker")
        time.sleep(interval_seconds)


def receive_once(settings: Settings, *, timeout_seconds: int = 260) -> None:
    with single_instance_lock("receive") as locked:
        if not locked:
            return
        session_factory = session_factory_for(settings)
        try:
            received = receive_once_via_croc(settings, timeout_seconds=timeout_seconds)
            with session_scope(session_factory) as session:
                results = import_received_json_files(session)
            log_line(f"Receive once: received={len(received)} imported={len(results)}")
        except subprocess.TimeoutExpired:
            log_line("Receive once: timeout, no file.")
        except Exception as exc:
            log_line(f"Receive once error: {exc}")


def receive_worker(settings: Settings) -> None:
    session_factory = session_factory_for(settings)
    while True:
        try:
            with single_instance_lock("receive") as locked:
                if not locked:
                    time.sleep(30)
                    continue
                received = receive_once_via_croc(settings)
                with session_scope(session_factory) as session:
                    results = import_received_json_files(session)
                log_line(f"Receive worker: received={len(received)} imported={len(results)}")
        except Exception as exc:
            log_line(f"Receive worker error: {exc}")
            time.sleep(30)


def log_send_result(result, *, prefix: str) -> None:
    if result is None:
        log_line(f"{prefix}: queue is empty.")
        return
    statuses = ", ".join(f"{target.user_id}:{'ok' if target.success else 'fail'}" for target in result.targets)
    log_line(f"{prefix}: {statuses or 'no targets'}; synced={len(result.synced_item_ids)}")


@contextmanager
def single_instance_lock(name: str, *, stale_after_seconds: int = 3600):
    DATA_DIR.joinpath("metadata").mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "metadata" / f"desktop_{name}.lock"
    if path.exists() and time.time() - path.stat().st_mtime > stale_after_seconds:
        path.unlink(missing_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log_line(f"{name} skipped: previous run is still active.")
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield True
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def launch_desktop_window(*, show_window: bool = True) -> None:
    try:
        session_factory_for(get_settings())
    except Exception as exc:
        message = startup_error_message(exc)
        log_line(f"Desktop startup check failed: {message}")
        if show_window:
            open_startup_error_window(message)
        return

    port = free_port(8001)
    url = f"http://127.0.0.1:{port}/bought"
    thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    thread.start()
    try:
        wait_for_server(url)
    except RuntimeError as exc:
        message = SERVER_START_ERRORS[-1] if SERVER_START_ERRORS else str(exc)
        log_line(f"Desktop startup failed: {message}")
        if show_window:
            open_startup_error_window(message)
        return
    if show_window:
        open_webview(url)
    else:
        while True:
            time.sleep(3600)


def run_server(port: int) -> None:
    try:
        from app.main import app

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception as exc:
        message = startup_error_message(exc)
        SERVER_START_ERRORS.append(message)
        log_line(f"Server thread error: {message}")


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_server(url: str, timeout: int = 90) -> None:
    started_at = time.time()
    while time.time() - started_at < timeout:
        if SERVER_START_ERRORS:
            raise RuntimeError(SERVER_START_ERRORS[-1])
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("CRM server did not start in time")


def open_webview(url: str) -> None:
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        while True:
            time.sleep(3600)
    webview.create_window(APP_NAME, url, width=1360, height=900, min_size=(1024, 700))
    webview.start()


def startup_error_message(exc: Exception) -> str:
    raw = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    text = str(exc)
    if "connection" in text.casefold() or "could not connect" in text.casefold() or "connection refused" in text.casefold():
        return (
            "CRM не смогла подключиться к PostgreSQL. Проверьте, что PostgreSQL установлен и запущен, "
            "а логин/пароль/DB_NAME в файле .env указаны правильно. "
            f"Технически: {raw}"
        )
    if "connection" in text.casefold() or "could not connect" in text.casefold() or "connection refused" in text.casefold():
        return (
            "CRM не смогла подключиться к PostgreSQL. Проверьте, что PostgreSQL установлен и запущен, "
            "а логин/пароль/DB_NAME в файле .env указаны правильно. "
            f"Технически: {raw}"
        )
    return raw


def open_startup_error_window(message: str) -> None:
    DATA_DIR.joinpath("metadata").mkdir(parents=True, exist_ok=True)
    env_path = RUNTIME_DIR / ".env"
    path = DATA_DIR / "metadata" / "startup_error.html"
    body = html.escape(message)
    env_label = html.escape(str(env_path))
    path.write_text(
        f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>CRM не запустилась</title>
<body style="font-family:Segoe UI,Arial,sans-serif;background:#0f141b;color:#f5f7fb;margin:0;padding:32px;">
<h1 style="font-size:28px;margin:0 0 20px;">CRM не запустилась</h1>
<div style="background:#161d26;border:1px solid #2a3544;border-radius:8px;padding:18px;line-height:1.5;white-space:pre-wrap;">{body}</div>
<p style="color:#9fb2c8;margin-top:20px;">Конфиг: <code>{env_label}</code></p>
<p style="color:#9fb2c8;">Обычно нужно запустить PostgreSQL или поправить пароль в <code>.env</code>.</p>
</body>
</html>
""",
        encoding="utf-8",
    )
    try:
        import webview
    except ImportError:
        webbrowser.open(path.as_uri())
        return
    webview.create_window("CRM startup error", path.as_uri(), width=860, height=520, min_size=(720, 420))
    webview.start()
    return

    DATA_DIR.joinpath("metadata").mkdir(parents=True, exist_ok=True)
    env_path = RUNTIME_DIR / ".env"
    path = DATA_DIR / "metadata" / "startup_error.html"
    body = html.escape(message)
    env_label = html.escape(str(env_path))
    path.write_text(
        f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<title>CRM не запустилась</title>
<body style="font-family:Segoe UI,Arial,sans-serif;background:#0f141b;color:#f5f7fb;margin:0;padding:32px;">
<h1 style="font-size:28px;margin:0 0 20px;">CRM не запустилась</h1>
<div style="background:#161d26;border:1px solid #2a3544;border-radius:8px;padding:18px;line-height:1.5;white-space:pre-wrap;">{body}</div>
<p style="color:#9fb2c8;margin-top:20px;">Конфиг: <code>{env_label}</code></p>
<p style="color:#9fb2c8;">Обычно нужно запустить PostgreSQL или поправить пароль в <code>.env</code>.</p>
</body>
</html>
""",
        encoding="utf-8",
    )
    try:
        import webview
    except ImportError:
        webbrowser.open(path.as_uri())
        return
    webview.create_window("CRM startup error", path.as_uri(), width=860, height=520, min_size=(720, 420))
    webview.start()


def install_windows_tasks(*, silent: bool = False) -> None:
    if os.name != "nt":
        return
    executable = task_executable()
    commands = [
        (
            f"{EXE_TASK_PREFIX} Build Export",
            "WEEKLY",
            None,
            ["WED,FRI"],
            "18:00",
            f"{executable} --sync-build-export",
        ),
        (
            f"{EXE_TASK_PREFIX} Send Worker",
            "MINUTE",
            "5",
            [],
            None,
            f"{executable} --sync-send-once --timeout-seconds 90",
        ),
        (
            f"{EXE_TASK_PREFIX} Receive Worker",
            "MINUTE",
            "5",
            [],
            None,
            f"{executable} --sync-receive-once --timeout-seconds 260",
        ),
    ]
    for task_name, schedule, modifier, days, start_time, command in commands:
        create_task(task_name, schedule, modifier=modifier, days=days, start_time=start_time, command=command)
    if not silent:
        log_line("Windows tasks installed.")


def task_executable() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{sys.executable}" -m app.desktop'


def create_task(
    task_name: str,
    schedule: str,
    *,
    modifier: str | None,
    days: list[str],
    start_time: str | None,
    command: str,
) -> None:
    args = ["schtasks", "/Create", "/F", "/TN", task_name, "/SC", schedule, "/TR", command]
    if modifier:
        args.extend(["/MO", modifier])
    if days:
        args.extend(["/D", ",".join(days)])
    if start_time:
        args.extend(["/ST", start_time])
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def log_line(message: str) -> None:
    DATA_DIR.joinpath("metadata").mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "metadata" / "desktop_sync.log"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    queue_state = "queue=1" if CURRENT_EXPORT_PATH.exists() else "queue=0"
    path.open("a", encoding="utf-8").write(f"[{timestamp}] {queue_state} {message}\n")


if __name__ == "__main__":
    main()
