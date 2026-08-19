# run_cli.py - CLI-лаунчер RAG-системы (полный аналог run_ui.py без графического интерфейса)
"""Управление RAG-системой из командной строки.

Дублирует все функции стартовой панели run_ui.py:
  * настройки (LLM провайдер/модель, параметры поиска, OCR, Telegram, Caddy) —
    чтение и запись в БД (таблица app_settings);
  * запуск/остановка/перезапуск веб-интерфейса (режимы local / external_ip / domain);
  * перезагрузка базы знаний RAG;
  * запуск/остановка Telegram-бота;
  * список пользователей и их персональные промты.

Запуск (лучше через venv проекта):
    venv\\Scripts\\python.exe run_cli.py [команда]
Без аргументов запускается интерактивное меню с пунктами («кнопками»);
построчный ввод команд — run_cli.py shell.

Примеры:
    python run_cli.py status
    python run_cli.py settings
    python run_cli.py settings set llm_provider DeepSeek
    python run_cli.py settings set llm_model deepseek-chat
    python run_cli.py settings set search_top_k 30
    python run_cli.py web start --mode local
    python run_cli.py web status
    python run_cli.py web stop
    python run_cli.py telegram start
    python run_cli.py users prompt get aquasegmentum
"""

import argparse
import http.client
import os
import shlex
import socket
import subprocess
import sys
import time
import webbrowser
from types import SimpleNamespace

# Windows-консоль (cmd/PowerShell) часто не в UTF-8 — переключаем вывод принудительно
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(PROJECT_DIR, ".run_cli")  # PID-файлы и логи процессов
os.makedirs(RUN_DIR, exist_ok=True)

# Fallback-копия маппинга провайдеров (используется, только если run_ui.py недоступен).
# Основной источник — PROVIDERS из run_ui.py (см. get_providers()).
_PROVIDERS_FALLBACK = {
    "DashScope": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["deepseek-v4-flash", "qwen-plus", "qwen-turbo", "qwen-max"],
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["qwen/qwen-plus", "deepseek/deepseek-chat", "openai/gpt-4o-mini"],
    },
}

# Ключи, значения которых маскируются в выводе (секреты)
_SECRET_KEYS = ("llm_api_key", "llm_provider_api_key", "llm_openrouter_api_key", "telegram_bot_token")

# Дефолты настроек — зеркало run_ui.load_settings()
_DEFAULTS = {
    "disable_llm_models": False,
    "disable_hybrid_search": False,
    "disable_knowledge_base_search": False,
    "llm_api_key": "",
    "llm_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "llm_provider": "DashScope",
    "llm_model": "deepseek-v4-flash",
    "search_top_k": 30,
    "search_alpha": 0.7,
    "relevance_threshold": 0.1,
    "max_context_fragments": 100,
    "telegram_bot_token": "",
    "llm_provider_api_key": "",
    "llm_openrouter_api_key": "",
    "ocr_enabled": False,
    "ocr_model": "qwen-vl-ocr",
    "ocr_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "ocr_dpi": 150,
    "caddy_path": (r"C:\Users\Petrlev\AppData\Local\Microsoft\WinGet\Packages"
                   r"\CaddyServer.Caddy_Microsoft.Winget.Source_8wekyb3d8bbwe\caddy.exe"
                   if os.name == "nt" else "/usr/bin/caddy"),
    "caddy_dir": PROJECT_DIR,
}

_OCR_MODELS = ["qwen-vl-ocr", "qwen-vl-plus", "qwen-vl-max"]

# Группировка настроек для показа в `settings` и для меню
SETTINGS_GROUPS = [
    ("Основные", ["disable_llm_models", "disable_hybrid_search", "disable_knowledge_base_search"]),
    ("Поиск", ["search_top_k", "search_alpha", "relevance_threshold", "max_context_fragments"]),
    ("API (LLM)", ["llm_provider", "llm_model", "llm_base_url", "llm_api_key",
                   "llm_provider_api_key", "llm_openrouter_api_key"]),
    ("OCR", ["ocr_enabled", "ocr_model", "ocr_base_url", "ocr_dpi"]),
    ("Telegram", ["telegram_bot_token"]),
    ("Caddy", ["caddy_path", "caddy_dir"]),
]

# Подсказки для ввода значений настроек в меню
_KEY_HINTS = {
    "search_top_k": "целое число от 1 до 50",
    "search_alpha": "число от 0.0 до 1.0 (0 — только ключевые слова, 1 — только семантика)",
    "relevance_threshold": "число от 0.0 до 1.0 (рекомендуется 0.1–0.3)",
    "max_context_fragments": "целое число от 10 до 500",
    "ocr_dpi": "целое число от 50 до 600 (рекомендуется 150)",
    "disable_llm_models": "true/false",
    "disable_hybrid_search": "true/false",
    "disable_knowledge_base_search": "true/false",
    "ocr_enabled": "true/false",
    "llm_provider": "DashScope | DeepSeek | OpenRouter",
    "llm_model": "например: qwen-turbo, deepseek-chat (для OpenRouter — вручную, напр. openai/gpt-4o)",
    "llm_base_url": "URL OpenAI-совместимого API",
    "llm_api_key": "ключ DashScope/OCR (при провайдере DeepSeek/OpenRouter сохранится в его поле)",
    "llm_provider_api_key": "ключ провайдера DeepSeek",
    "llm_openrouter_api_key": "ключ провайдера OpenRouter",
    "ocr_model": "qwen-vl-ocr | qwen-vl-plus | qwen-vl-max",
    "ocr_base_url": "URL OCR API (DashScope)",
    "telegram_bot_token": "токен от @BotFather",
    "caddy_path": "путь к исполняемому файлу caddy.exe",
    "caddy_dir": "папка с Caddyfile",
}

# Режимы запуска веб-интерфейса (для меню)
_WEB_MODES = [
    ("local", "Локальный (localhost:8077)"),
    ("external_ip", "Внешний IP (85.234.31.16:8077)"),
    ("domain", "Внешний домен (assistant.proaibro.ru) — Caddy"),
]

# Булевы настройки (в меню выбираются стрелками true/false)
_BOOL_KEYS = ("disable_llm_models", "disable_hybrid_search", "disable_knowledge_base_search", "ocr_enabled")

# Код, который исполняется в отдельном процессе Telegram-бота (токен читается из БД,
# чтобы не передавать секрет через аргументы командной строки)
_TELEGRAM_RUNNER = (
    "import sys, io\n"
    "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
    "sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')\n"
    "import logging\n"
    "logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)\n"
    "from telegram_bot import TelegramRAGBot\n"
    "from auth_db import get_all_settings\n"
    "token = ((get_all_settings() or {}).get('telegram_bot_token') or '').strip()\n"
    "if not token or len(token) < 20 or token == 'YOUR_BOT_TOKEN_HERE':\n"
    "    print('Токен Telegram-бота не задан в настройках', flush=True)\n"
    "    raise SystemExit(1)\n"
    "print('Telegram-бот запущен, ожидание сообщений...', flush=True)\n"
    "TelegramRAGBot(token).run()\n"
)


# =====================================================================
# Утилиты
# =====================================================================

def get_providers():
    """Маппинг провайдеров — берём из run_ui.py (единый источник), при недоступности — fallback."""
    try:
        from run_ui import PROVIDERS
        return PROVIDERS
    except Exception:
        return _PROVIDERS_FALLBACK


def mask_key(value):
    """Маскирование секрета: sk-12…2966. Пустое → '(не задан)'."""
    s = str(value or "")
    if not s:
        return "(не задан)"
    if len(s) <= 8:
        return "***"
    return f"{s[:5]}…{s[-4:]}"


def _parse_bool(value):
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "да", "вкл"):
        return True
    if s in ("0", "false", "no", "off", "нет", "выкл"):
        return False
    return None


def load_settings():
    """Настройки из БД (app_settings) + дефолты для недостающих ключей (как в run_ui)."""
    defaults = dict(_DEFAULTS)
    try:
        from auth_db import get_all_settings
        loaded = get_all_settings() or {}
        for key, value in defaults.items():
            if key not in loaded:
                loaded[key] = value
        return loaded
    except Exception as e:
        print(f"⚠️ БД недоступна, используются настройки по умолчанию: {e}")
        return defaults


def save_settings(mapping):
    """Сохранение набора настроек в БД (UPSERT). Возвращает True/False."""
    try:
        from auth_db import set_settings
        if set_settings(mapping):
            return True
        print("❌ Не удалось сохранить настройки в БД")
    except Exception as e:
        print(f"❌ Не удалось сохранить настройки: {e}")
    return False


def validate_setting(key, value):
    """Валидация значения настройки (зеркало validate_and_apply_settings из run_ui).
    Возвращает (нормализованное_значение, предупреждение_или_None).
    При неверном типе/диапазоне возвращает (None, ошибка) — значение не сохраняется."""
    if key in ("search_top_k", "max_context_fragments", "ocr_dpi"):
        try:
            v = int(str(value).strip())
        except (TypeError, ValueError):
            return None, f"ожидается целое число, получено: {value!r}"
        if key == "search_top_k" and not (1 <= v <= 50):
            return None, "TOP_K должен быть от 1 до 50"
        if key == "max_context_fragments" and not (10 <= v <= 500):
            return None, "максимум фрагментов должен быть от 10 до 500"
        if key == "ocr_dpi" and not (50 <= v <= 600):
            return None, "OCR DPI должен быть от 50 до 600"
        return v, None
    if key in ("search_alpha", "relevance_threshold"):
        try:
            v = float(str(value).strip())
        except (TypeError, ValueError):
            return None, f"ожидается число, получено: {value!r}"
        if not (0.0 <= v <= 1.0):
            return None, "значение должно быть в диапазоне 0.0–1.0"
        return v, None
    if key in ("disable_llm_models", "disable_hybrid_search", "disable_knowledge_base_search", "ocr_enabled"):
        v = _parse_bool(value)
        if v is None:
            return None, "ожидается true/false (1/0, yes/no, on/off)"
        return v, None
    if key == "llm_provider":
        v = str(value).strip()
        if v not in get_providers():
            return None, f"неизвестный провайдер. Доступны: {', '.join(get_providers())}"
        return v, None
    if key == "llm_model":
        v = str(value).strip()
        if not v:
            return None, "модель не может быть пустой"
        return v, None
    if key == "ocr_model":
        v = str(value).strip()
        if not v:
            return None, "OCR-модель не может быть пустой"
        if v not in _OCR_MODELS:
            print(f"  ⚠️ Нестандартная OCR-модель: {v} (обычно: {', '.join(_OCR_MODELS)})")
        return v, None
    if key in ("llm_base_url", "llm_api_key", "llm_provider_api_key", "llm_openrouter_api_key",
               "telegram_bot_token", "ocr_base_url", "caddy_path", "caddy_dir"):
        return str(value).strip(), None
    return None, f"неизвестный ключ настройки: {key}. Список ключей — в `settings` (без аргументов)"


def apply_key_routing(key, value):
    """Куда реально ложится значение ключа llm_api_key — зеркало run_ui.validate_and_apply_settings:
    при провайдере DeepSeek → llm_provider_api_key, OpenRouter → llm_openrouter_api_key,
    иначе → llm_api_key. Остальные ключи сохраняются как есть."""
    if key != "llm_api_key":
        return key
    provider = load_settings().get("llm_provider", "DashScope")
    if provider == "DeepSeek":
        return "llm_provider_api_key"
    if provider == "OpenRouter":
        return "llm_openrouter_api_key"
    return "llm_api_key"


# =====================================================================
# Управление фоновыми процессами (PID-файлы + логи в .run_cli/)
# =====================================================================

def _pid_path(name):
    return os.path.join(RUN_DIR, f"{name}.pid")


def _write_pid(name, pid):
    with open(_pid_path(name), "w", encoding="utf-8") as f:
        f.write(str(pid))


def _remove_pid(name):
    try:
        os.remove(_pid_path(name))
    except OSError:
        pass


def _read_pid(name):
    try:
        with open(_pid_path(name), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _running(name, label):
    """True, если процесс по PID-файлу жив. Устаревший PID-файл подчищается."""
    pid = _read_pid(name)
    if pid is None:
        return False
    if not _pid_alive(pid):
        _remove_pid(name)
        return False
    print(f"  {label} уже запущен (PID {pid})")
    return True


def _spawn(cmd, log_name, cwd=None, env=None):
    """Запуск фонового процесса: stdout/stderr → .run_cli/<log_name>, окно не показываем."""
    log_path = os.path.join(RUN_DIR, log_name)
    log_file = open(log_path, "ab")  # handle наследуется дочерним процессом
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    proc = subprocess.Popen(
        cmd,
        cwd=cwd or PROJECT_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    return proc.pid


def _stop_process(name, label):
    """Остановка процесса по PID-файлу (Windows: taskkill /T /F — весь дерево).
    Возвращает True, если процесс был остановлен."""
    pid = _read_pid(name)
    if pid is None:
        return False
    if not _pid_alive(pid):
        _remove_pid(name)
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            try:
                os.kill(pid, 15)  # SIGTERM
            except ProcessLookupError:
                pass
    except Exception as e:
        print(f"  ⚠️ {label}: ошибка остановки PID {pid}: {e}")
    _remove_pid(name)
    print(f"  {label}: остановлен (PID {pid})")
    return True


def _log_path(log_name):
    return os.path.join(RUN_DIR, log_name)


def _print_log_tail(log_name, lines=15):
    path = _log_path(log_name)
    if not os.path.exists(path):
        print("  (лога нет)")
        return
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4000))
            data = f.read().decode("utf-8", errors="replace")
        tail = data.strip().splitlines()[-lines:]
        for line in tail:
            print(f"  | {line}")
    except OSError as e:
        print(f"  (не удалось прочитать лог: {e})")


def _resolve_web_python():
    """(python, env) для запуска run_web.py — точная копия run_ui._resolve_web_python.
    Windows: uv python с явным PYTHONPATH на site-packages venv проекта (+ hermes — там fitz).
    Linux: venv/bin/python."""
    env = dict(os.environ)
    if os.name == "nt":
        venv_sp = os.path.join(PROJECT_DIR, "venv", "Lib", "site-packages")
        hermes_sp = os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes",
                                 "hermes-agent", "venv", "Lib", "site-packages")
        paths = [venv_sp]
        if os.path.isdir(hermes_sp):
            paths.append(hermes_sp)
        env["PYTHONPATH"] = os.pathsep.join(paths)
        uv_python = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "uv", "python",
                                 "cpython-3.11-windows-x86_64-none", "python.exe")
        if os.path.exists(uv_python):
            return uv_python, env
        return sys.executable, env
    venv_python = os.path.join(PROJECT_DIR, "venv", "bin", "python")
    if os.path.exists(venv_python):
        return venv_python, env
    return sys.executable, env


def is_web_server_running():
    """True, если на порту 8077 отвечает веб-приложение (HTTP-проверка, как в run_ui)."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8077, timeout=1)
        try:
            conn.request("GET", "/login")
            conn.getresponse()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def is_caddy_admin_running():
    """True, если админ-эндпоинт Caddy (127.0.0.1:2019) уже слушается —
    значит Caddy запущен вне CLI (например, systemd на сервере)."""
    try:
        conn = socket.create_connection(("127.0.0.1", 2019), timeout=0.5)
        conn.close()
        return True
    except OSError:
        return False


def _wait_for_web(timeout=90.0):
    """Ожидание ответа веб-сервера на порту 8077. Возвращает True/False.
    Эмбеддинг-модель при первом запуске грузится до минуты, поэтому таймаут щедрый."""
    deadline = time.time() + timeout
    last_report = time.time()
    while time.time() < deadline:
        if is_web_server_running():
            return True
        time.sleep(1.0)
        if time.time() - last_report >= 10:
            remaining = int(deadline - time.time())
            print(f"  ⏳ Ожидание ответа сервера... осталось ~{remaining} c (идёт загрузка RAG)")
            last_report = time.time()
    return False


# =====================================================================
# Ленивая RAG-система (как self.rag_system в run_ui)
# =====================================================================

_rag = None


def get_rag_system():
    """Глобальный RAGCore этого процесса (тяжёлый: эмбеддинг-модель ~2 ГБ)."""
    global _rag
    if _rag is None:
        from rag_core import get_rag_system as _grs
        _rag = _grs()
        for key, value in load_settings().items():
            _rag.settings.set(key, value)
        _rag._setup_client()
    return _rag


def _apply_settings_to_rag_if_loaded():
    """Применение настроек к уже загруженной RAG-системе (аналог кнопки «Применить»)."""
    if _rag is None:
        print("  ℹ️ RAG-система в этом процессе не загружена — настройки будут подхвачены при старте.")
        return
    for key, value in load_settings().items():
        _rag.settings.set(key, value)
    _rag._setup_client()
    print("  ✅ Настройки применены к загруженной RAG-системе (клиент пересоздан)")


def _resolve_user(user_ref):
    """Пользователь по ID или имени пользователя. Возвращает dict из БД или None."""
    try:
        from auth_db import get_all_users
        users = get_all_users() or []
    except Exception as e:
        print(f"❌ Не удалось получить список пользователей: {e}")
        return None
    for u in users:
        if str(u["id"]) == str(user_ref) or u["username"] == user_ref:
            return u
    print(f"❌ Пользователь не найден: {user_ref}")
    print(f"   Доступные: {', '.join(u['username'] for u in users) or '(нет)'}")
    return None


# =====================================================================
# Команды
# =====================================================================

def cmd_status():
    """Общий статус системы (лёгкий: БД + порт + PID-файлы, без загрузки RAG)."""
    print("═══ RAG-система: статус ═══")
    settings = load_settings()
    provider = settings.get("llm_provider", "?")
    model = settings.get("llm_model", "?")
    print(f"Провайдер/модель: {provider} / {model}")
    flags = []
    if settings.get("disable_llm_models"):
        flags.append("LLM отключены")
    if settings.get("disable_hybrid_search"):
        flags.append("гибридный поиск выключен")
    if settings.get("disable_knowledge_base_search"):
        flags.append("поиск по БЗ выключен")
    print(f"Флаги: {', '.join(flags) if flags else 'все включены'}")
    print()

    web_pid = _read_pid("web")
    if is_web_server_running():
        if web_pid and _pid_alive(web_pid):
            print(f"Веб-интерфейс (порт 8077): 🟢 запущен (наш PID {web_pid})")
        else:
            print("Веб-интерфейс (порт 8077): 🟡 запущен ВНЕ CLI (внешний процесс)")
            if web_pid:
                _remove_pid("web")
    else:
        print("Веб-интерфейс (порт 8077): ⚪ не запущен")

    caddy_pid = _read_pid("caddy")
    if caddy_pid and _pid_alive(caddy_pid):
        print(f"Caddy: 🟢 запущен (PID {caddy_pid})")
    elif is_caddy_admin_running():
        print("Caddy: 🟡 запущен ВНЕ CLI (админ-порт 127.0.0.1:2019 занят)")
        if caddy_pid:
            _remove_pid("caddy")
    else:
        if caddy_pid:
            _remove_pid("caddy")
        print("Caddy: ⚪ не запущен")

    tg_pid = _read_pid("telegram")
    if tg_pid and _pid_alive(tg_pid):
        print(f"Telegram-бот: 🟢 запущен (PID {tg_pid})")
    else:
        if tg_pid:
            _remove_pid("telegram")
        tg_token = settings.get("telegram_bot_token") or ""
        if tg_token and len(tg_token) >= 20:
            print("Telegram-бот: ⚪ не запущен (токен задан)")
        else:
            print("Telegram-бот: ⚪ не запущен (токен не задан)")

    try:
        from auth_db import get_all_users
        users = get_all_users() or []
        print(f"\nПользователей в БД: {len(users)}")
    except Exception as e:
        print(f"\nПользователей в БД: не удалось получить ({e})")
    print(f"Логи процессов: {RUN_DIR}")
    return 0


def cmd_settings(args):
    """settings [show|get|set|reset] — настройки из БД app_settings."""
    action = args.action if args.action is not None else "show"

    if action == "show":
        settings = load_settings()
        print("═══ Настройки (БД app_settings + дефолты) ═══")
        for title, keys in SETTINGS_GROUPS:
            print(f"\n── {title} ──")
            for key in keys:
                value = settings.get(key, "")
                display = mask_key(value) if (key in _SECRET_KEYS and not args.full) else value
                if key in _SECRET_KEYS and not args.full:
                    display += "  (--full для полного значения)"
                print(f"  {key} = {display}")
        print("\nПодсказка: settings set <ключ> <значение> | settings get <ключ> | settings reset <ключ>")
        return 0

    if action == "get":
        if not args.key:
            print("❌ Укажите ключ: settings get <ключ>")
            return 2
        settings = load_settings()
        if args.key in settings:
            display = mask_key(settings[args.key]) if (args.key in _SECRET_KEYS and not args.full) else settings[args.key]
            print(f"{args.key} = {display}")
            return 0
        if args.key in _DEFAULTS:
            print(f"{args.key} = (не задан; по умолчанию: {_DEFAULTS[args.key]})")
            return 0
        print(f"❌ Неизвестный ключ: {args.key}")
        return 2

    if action == "reset":
        if not args.key:
            print("❌ Укажите ключ: settings reset <ключ>")
            return 2
        if args.key not in _DEFAULTS:
            print(f"❌ Нет значения по умолчанию для ключа: {args.key}")
            return 2
        target = _DEFAULTS[args.key]
        if save_settings({args.key: target}):
            print(f"✅ {args.key} сброшен к значению по умолчанию: {target}")
            return 0
        return 1

    # action == "set"
    if not args.key or args.value is None:
        print("❌ Использование: settings set <ключ> <значение>")
        print("   Например: settings set llm_provider DeepSeek")
        return 2
    key, value = args.key, args.value

    normalized, error = validate_setting(key, value)
    if error:
        print(f"❌ {key}: {error}")
        return 2

    target_key = apply_key_routing(key, normalized)
    if save_settings({target_key: normalized}):
        display = mask_key(normalized) if (target_key in _SECRET_KEYS and not args.full) else normalized
        routed = f" (сохранено как {target_key})" if target_key != key else ""
        print(f"✅ {key} = {display}{routed}")
        if args.apply:
            _apply_settings_to_rag_if_loaded()
        else:
            print("  ℹ️ Для применения к загруженной RAG-системе используйте: settings set ... --apply")
        return 0
    return 1


def cmd_info():
    """Информация о базе знаний (загружает RAG-систему, как кнопка «Обновить информацию» в GUI)."""
    print("⏳ Загрузка RAG-системы (эмбеддинг-модель ~2 ГБ, может занять 1–2 минуты)...")
    try:
        rag = get_rag_system()
        knowledge_count = len(rag.my_knowledge) if rag.my_knowledge else 0
        files_count = len(rag.all_knowledge_dict) if rag.all_knowledge_dict else 0
        settings = load_settings()
        print(f"Фрагментов в базе знаний: {knowledge_count}")
        print(f"Загружено файлов: {files_count}")
        print(f"Провайдер/модель: {settings.get('llm_provider')} / {settings.get('llm_model')}")
        return 0
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return 1


def cmd_web_status():
    """Статус веб-интерфейса и Caddy."""
    print("═══ Веб-интерфейс ═══")
    web_pid = _read_pid("web")
    if is_web_server_running():
        if web_pid and _pid_alive(web_pid):
            print(f"Веб-сервер (порт 8077): запущен, PID {web_pid}")
        else:
            print("Веб-сервер (порт 8077): запущен ВНЕ CLI (внешний процесс)")
            if web_pid:
                _remove_pid("web")
    else:
        if web_pid and _pid_alive(web_pid):
            print(f"Веб-сервер: наш процесс PID {web_pid} жив, но порт 8077 не отвечает.")
            print("  Последние строки лога:")
            _print_log_tail("web.log")
        else:
            print("Веб-сервер (порт 8077): не запущен")

    caddy_pid = _read_pid("caddy")
    if caddy_pid and _pid_alive(caddy_pid):
        print(f"Caddy: запущен, PID {caddy_pid}")
    elif is_caddy_admin_running():
        print("Caddy: запущен ВНЕ CLI (админ-порт 127.0.0.1:2019 занят)")
        if caddy_pid:
            _remove_pid("caddy")
    else:
        if caddy_pid:
            _remove_pid("caddy")
        print("Caddy: не запущен")
    return 0


def _start_caddy():
    settings = load_settings()
    caddy_path = settings.get("caddy_path", "")
    caddy_dir = settings.get("caddy_dir") or PROJECT_DIR
    if not caddy_path or not os.path.exists(caddy_path):
        print(f"❌ Caddy не найден: {caddy_path!r}")
        print("   Укажите путь: python run_cli.py settings set caddy_path <путь к caddy.exe>")
        return None
    if _running("caddy", "Caddy"):
        return _read_pid("caddy")
    # Caddy уже работает вне CLI (systemd на сервере / запущен вручную) —
    # второй экземпляр упадёт на занятом админ-порту 2019, поэтому не запускаем дубль
    if is_caddy_admin_running():
        print("  ℹ️ Caddy уже запущен вне CLI (админ-порт 127.0.0.1:2019 занят) — используем существующий")
        return "external"
    pid = _spawn([caddy_path, "run"], "caddy.log", cwd=caddy_dir)
    _write_pid("caddy", pid)
    time.sleep(1.0)
    if not _pid_alive(pid):
        print(f"  ⚠️ Caddy завершился сразу после запуска (PID {pid}). Последние строки лога:")
        _print_log_tail("caddy.log")
        _remove_pid("caddy")
        return None
    print(f"  Caddy запущен (PID {pid}), лог: {_log_path('caddy.log')}")
    return pid


def cmd_web_start(mode, open_browser=False):
    """Запуск веб-интерфейса в выбранном режиме (аналог launch_web в run_ui)."""
    if _running("web", "Веб-интерфейс"):
        return 0
    if is_web_server_running():
        print("⚠️ Порт 8077 уже занят — веб-сервер запущен ВНЕ CLI (например, вручную).")
        print("   Чтобы запустить сервер отсюда, сначала остановите внешний процесс.")
        return 1

    # Перед запуском сохраняем текущие настройки (как делает GUI в launch_web)
    save_settings(load_settings())

    if mode == "domain":
        if _start_caddy() is None:
            return 1

    python_exe, env = _resolve_web_python()
    pid = _spawn([python_exe, "-u", os.path.join(PROJECT_DIR, "run_web.py")], "web.log", env=env)
    _write_pid("web", pid)

    urls = {
        "local": "http://localhost:8077",
        "external_ip": "http://85.234.31.16:8077",
        "domain": "https://assistant.proaibro.ru",
    }
    mode_labels = {"local": "Локальный", "external_ip": "Внешний IP", "domain": "Внешний домен"}
    ok = _wait_for_web(timeout=10)
    if ok:
        print(f"✅ Веб-интерфейс запущен: {mode_labels[mode]} (PID {pid})")
        print(f"   URL: {urls[mode]}")
    else:
        print(f"⚠️ Процесс запущен (PID {pid}), но сервер так и не ответил на порту 8077.")
        print("   Последние строки лога:")
        _print_log_tail("web.log")
    print(f"   Лог: {_log_path('web.log')}")
    if open_browser:
        def _open():
            time.sleep(1)
            webbrowser.open(urls[mode])
        import threading
        threading.Thread(target=_open, daemon=True).start()
    return 0 if ok else 1


def cmd_web_stop():
    """Остановка веб-интерфейса и Caddy (только свои процессы)."""
    stopped = []
    if _stop_process("web", "Веб-интерфейс"):
        stopped.append("Веб-интерфейс")
    if _stop_process("caddy", "Caddy"):
        stopped.append("Caddy")
    elif is_caddy_admin_running():
        print("  ℹ️ Caddy работает вне CLI — оставляем как есть")
    if is_web_server_running():
        print("  ⚠️ Порт 8077 всё ещё отвечает — веб-сервер запущен ВНЕ CLI. Остановите его вручную.")
    if not stopped:
        print("Ничего не запущено")
    return 0


def cmd_web_restart(mode, open_browser=False):
    """Перезапуск веб-интерфейса: stop + start."""
    print("🔄 Перезапуск веб-интерфейса...")
    cmd_web_stop()
    return cmd_web_start(mode, open_browser=open_browser)


def cmd_rag_restart():
    """Перезагрузка базы знаний (аналог restart_rag в run_ui)."""
    print("🔄 Перезагрузка базы знаний RAG...")
    print("   (Загружается RAG-система — эмбеддинг-модель ~2 ГБ, может занять 1–2 минуты)")
    try:
        rag = get_rag_system()
        rag.reload_knowledge_base()
        knowledge_count = len(rag.my_knowledge) if rag.my_knowledge else 0
        files_count = len(rag.all_knowledge_dict) if rag.all_knowledge_dict else 0
        print(f"✅ База знаний перезагружена: фрагментов {knowledge_count}, файлов {files_count}")
        return 0
    except Exception as e:
        print(f"❌ Ошибка перезагрузки RAG: {e}")
        return 1


def cmd_telegram_status():
    tg_pid = _read_pid("telegram")
    settings = load_settings()
    token = settings.get("telegram_bot_token") or ""
    if tg_pid and _pid_alive(tg_pid):
        print(f"Telegram-бот: запущен (PID {tg_pid})")
    else:
        if tg_pid:
            _remove_pid("telegram")
        print("Telegram-бот: не запущен")
    if token and len(token) >= 20:
        print(f"Токен: задан ({mask_key(token)})")
    else:
        print("Токен: не задан (settings set telegram_bot_token <TOKEN>)")
    return 0


def cmd_telegram_start():
    """Запуск Telegram-бота в фоне (аналог start_telegram_bot в run_ui)."""
    settings = load_settings()
    token = settings.get("telegram_bot_token") or ""
    if not token or len(token) < 20 or token == "YOUR_BOT_TOKEN_HERE":
        print("❌ Не задан валидный Telegram Bot Token.")
        print("   Сохраните: python run_cli.py settings set telegram_bot_token <TOKEN>")
        return 1
    if _running("telegram", "Telegram-бот"):
        return 0

    python_exe, env = _resolve_web_python()
    pid = _spawn([python_exe, "-u", "-c", _TELEGRAM_RUNNER], "telegram.log", env=env)
    _write_pid("telegram", pid)
    time.sleep(3)
    if _pid_alive(pid):
        print(f"✅ Telegram-бот запущен (PID {pid})")
        print(f"   Лог: {_log_path('telegram.log')}")
        return 0
    print("❌ Telegram-бот завершился сразу после запуска. Последние строки лога:")
    _print_log_tail("telegram.log")
    _remove_pid("telegram")
    return 1


def cmd_telegram_stop():
    if _stop_process("telegram", "Telegram-бот"):
        print("  Проверьте лог: " + _log_path("telegram.log"))
    else:
        print("Telegram-бот не запущен")
    return 0


def cmd_users_list():
    """Список пользователей (аналог вкладки «Пользователи» в run_ui)."""
    try:
        from auth_db import get_all_users
        users = get_all_users()
    except Exception as e:
        print(f"❌ Не удалось получить список пользователей: {e}")
        return 1
    if not users:
        print("Пользователи не найдены (или БД недоступна)")
        return 0
    print(f"{'ID':<5}{'Имя пользователя':<24}{'Папка в Database':<28}Пароль")
    print("-" * 80)
    for u in users:
        user_id = u["id"]
        folder = f"user_{user_id}"
        folder_path = os.path.join(PROJECT_DIR, "Database", folder)
        folder_display = folder + ("  ✅" if os.path.isdir(folder_path) else "  (нет папки)")
        print(f"{user_id:<5}{u['username']:<24}{folder_display:<28}(хэш — не восстанавливается)")
    print(f"\nВсего пользователей: {len(users)}")
    return 0


def cmd_users_prompt_get(user_ref, show_default=False):
    user = _resolve_user(user_ref)
    if not user:
        return 1
    try:
        from auth_db import get_user_prompt
        prompt = get_user_prompt(user["id"])
    except Exception as e:
        print(f"❌ Ошибка загрузки промта: {e}")
        return 1
    print(f"Пользователь: {user['username']} (ID {user['id']})")
    if prompt:
        print("Персональный промт:")
        print("---")
        print(prompt)
        print("---")
    else:
        print("Персональный промт не задан — используется стандартный системный промт")
        print("(DEFAULT_BASE_PROMPT в rag_core.py)")
        if show_default:
            try:
                from rag_core import DEFAULT_BASE_PROMPT
                print("Стандартный промт:")
                print("---")
                print(DEFAULT_BASE_PROMPT.strip())
                print("---")
            except Exception as e:
                print(f"  (не удалось показать стандартный промт: {e})")
    return 0


def cmd_users_prompt_set(user_ref, text=None, path=None):
    user = _resolve_user(user_ref)
    if not user:
        return 1
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"❌ Не удалось прочитать файл {path}: {e}")
            return 1
    text = (text or "").strip()
    # Текст стандартного промта = сброс к системному промту (как в run_ui)
    try:
        from auth_db import set_user_prompt
        from rag_core import DEFAULT_BASE_PROMPT
        if text and text == DEFAULT_BASE_PROMPT.strip():
            text = ""
        if set_user_prompt(user["id"], text):
            print(f"✅ Промт пользователя {user['username']} "
                  f"{'сброшен к стандартному' if not text else 'сохранён'}")
            return 0
        print("❌ Ошибка сохранения в БД")
        return 1
    except Exception as e:
        print(f"❌ Ошибка сохранения промта: {e}")
        return 1


def cmd_users_prompt_reset(user_ref):
    user = _resolve_user(user_ref)
    if not user:
        return 1
    try:
        from auth_db import set_user_prompt
        if set_user_prompt(user["id"], ""):
            print(f"✅ Промт пользователя {user['username']} сброшен к стандартному")
            return 0
        print("❌ Ошибка сброса в БД")
        return 1
    except Exception as e:
        print(f"❌ Ошибка сброса промта: {e}")
        return 1


def cmd_stop_all():
    """Остановка всего: веб-интерфейс + Caddy + Telegram-бот (аналог stop_all в run_ui)."""
    stopped = []
    if _stop_process("web", "Веб-интерфейс"):
        stopped.append("Веб-интерфейс")
    if _stop_process("caddy", "Caddy"):
        stopped.append("Caddy")
    if _stop_process("telegram", "Telegram-бот"):
        stopped.append("Telegram-бот")
    if is_web_server_running():
        print("  ⚠️ Порт 8077 всё ещё отвечает — веб-сервер запущен ВНЕ CLI. Остановите его вручную.")
    if stopped:
        print(f"🛑 Остановлено: {', '.join(stopped)}")
    else:
        print("Ничего не запущено")
    return 0


# =====================================================================
# Парсер и диспетчер
# =====================================================================

def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_cli.py",
        description="CLI-лаунчер RAG-системы — полный аналог run_ui.py без графического интерфейса.",
        epilog="Без аргументов запускается интерактивное меню (или shell — построчный режим). "
               "Рекомендуется запускать через venv: venv\\Scripts\\python.exe run_cli.py",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Общий статус: провайдер/модель, веб, Caddy, Telegram, пользователи")
    sub.add_parser("menu", help="Интерактивное меню с пунктами (по умолчанию при запуске без аргументов)")
    sub.add_parser("shell", help="Построчный интерактивный режим (ввод команд вручную)")

    sp = sub.add_parser("settings", help="Настройки: show (по умолчанию) / get / set / reset")
    sp.add_argument("action", nargs="?", choices=["show", "get", "set", "reset"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--full", action="store_true", help="Показывать полные значения секретов")
    sp.add_argument("--apply", action="store_true",
                    help="Применить настройки к загруженной RAG-системе (как кнопка «Применить»)")

    sub.add_parser("info", help="Информация о базе знаний (загружает RAG, ~2 ГБ RAM)")

    wsp = sub.add_parser("web", help="Веб-интерфейс: start / stop / restart / status")
    web_sub = wsp.add_subparsers(dest="web_action", required=True)
    ws = web_sub.add_parser("start", help="Запустить веб-интерфейс (режим по умолчанию: local)")
    ws.add_argument("--mode", choices=["local", "external_ip", "domain"], default="local",
                    help="local — localhost:8077; external_ip — 85.234.31.16:8077; domain — Caddy + assistant.proaibro.ru")
    ws.add_argument("--open", action="store_true", help="Открыть браузер после запуска")
    wr = web_sub.add_parser("restart", help="Перезапустить веб-интерфейс (stop + start)")
    wr.add_argument("--mode", choices=["local", "external_ip", "domain"], default="local")
    wr.add_argument("--open", action="store_true")
    web_sub.add_parser("stop", help="Остановить веб-интерфейс и Caddy")
    web_sub.add_parser("status", help="Статус веб-интерфейса и Caddy")

    rgp = sub.add_parser("rag", help="RAG: restart — перезагрузка базы знаний")
    rgp.add_subparsers(dest="rag_action", required=True).add_parser("restart", help="Перезагрузить базу знаний")

    tgp = sub.add_parser("telegram", help="Telegram-бот: start / stop / status")
    tg_sub = tgp.add_subparsers(dest="tg_action", required=True)
    tg_sub.add_parser("start", help="Запустить бота (токен берётся из настроек)")
    tg_sub.add_parser("stop", help="Остановить бота")
    tg_sub.add_parser("status", help="Статус бота")

    up = sub.add_parser("users", help="Пользователи: list / prompt")
    users_sub = up.add_subparsers(dest="users_action", required=True)
    users_sub.add_parser("list", help="Список пользователей")
    pp = users_sub.add_parser("prompt", help="Персональный промт: get / set / reset")
    prompt_sub = pp.add_subparsers(dest="prompt_action", required=True)
    pg = prompt_sub.add_parser("get", help="Показать промт пользователя (по ID или имени)")
    pg.add_argument("user")
    pg.add_argument("--show-default", action="store_true", help="Показать текст стандартного промта")
    ps = prompt_sub.add_parser("set", help="Установить промт пользователя (текст или файл)")
    ps.add_argument("user")
    ps.add_argument("--text", help="Текст промта (пустой текст = сброс к стандартному)")
    ps.add_argument("--file", help="Файл с текстом промта (UTF-8)")
    pr = prompt_sub.add_parser("reset", help="Сбросить промт пользователя к стандартному")
    pr.add_argument("user")

    sub.add_parser("stop", help="Остановить всё: веб-интерфейс + Caddy + Telegram-бот")
    return parser


def run_command(args):
    """Выполнение разобранной команды. Возвращает код возврата."""
    cmd = args.command
    if cmd == "status":
        return cmd_status()
    if cmd == "settings":
        return cmd_settings(args)
    if cmd == "info":
        return cmd_info()
    if cmd == "web":
        if args.web_action == "start":
            return cmd_web_start(args.mode, open_browser=args.open)
        if args.web_action == "stop":
            return cmd_web_stop()
        if args.web_action == "restart":
            return cmd_web_restart(args.mode, open_browser=args.open)
        if args.web_action == "status":
            return cmd_web_status()
    if cmd == "rag":
        return cmd_rag_restart()
    if cmd == "telegram":
        if args.tg_action == "start":
            return cmd_telegram_start()
        if args.tg_action == "stop":
            return cmd_telegram_stop()
        if args.tg_action == "status":
            return cmd_telegram_status()
    if cmd == "users":
        if args.users_action == "list":
            return cmd_users_list()
        if args.prompt_action == "get":
            return cmd_users_prompt_get(args.user, show_default=args.show_default)
        if args.prompt_action == "set":
            return cmd_users_prompt_set(args.user, text=args.text, path=args.file)
        if args.prompt_action == "reset":
            return cmd_users_prompt_reset(args.user)
    if cmd == "stop":
        return cmd_stop_all()
    print("Неизвестная команда. Введите help для списка.")
    return 2


_REPL_HELP = """Доступные команды (аналог run_ui.py):
  status                    — общий статус системы
  settings                  — показать все настройки
  settings get <ключ>       — значение настройки
  settings set <ключ> <знач> — установить и сохранить в БД (--apply — применить к RAG)
  settings reset <ключ>     — вернуть значение по умолчанию
  info                      — информация о базе знаний (загружает RAG)
  web start [--mode local|external_ip|domain] [--open] — запустить веб-интерфейс
  web stop                  — остановить веб-интерфейс и Caddy
  web restart [--mode ...]  — перезапустить веб-интерфейс
  web status                — статус веб-интерфейса и Caddy
  rag restart               — перезагрузка базы знаний
  telegram start|stop|status — управление Telegram-ботом
  users list                — список пользователей
  users prompt get <user> [--show-default]  — промт пользователя
  users prompt set <user> [--text ...|--file путь]  — установить промт
  users prompt reset <user> — сбросить промт к стандартному
  stop                      — остановить всё (веб + Caddy + Telegram)
  help                      — этот список
  exit / quit               — выход (Ctrl+C тоже работает)
"""


def interactive_loop(parser):
    """Интерактивный режим: команды вводятся построчно."""
    print("🚀 RAG-система — CLI-лаунчер (аналог run_ui.py)")
    print("Введите help для списка команд, exit — для выхода.")
    print()
    interactive = sys.stdin.isatty()
    while True:
        try:
            try:
                line = input("rag> " if interactive else "")
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            if line == "help":
                print(_REPL_HELP)
                continue
            try:
                args = parser.parse_args(shlex.split(line))
            except SystemExit:
                continue  # argparse уже напечатал ошибку/usage
            try:
                run_command(args)
            except KeyboardInterrupt:
                print("\n(команда прервана)")
            print()
        except KeyboardInterrupt:
            print()
            break
    _maybe_stop_on_exit()


def _maybe_stop_on_exit():
    """При выходе из интерактивного режима спрашиваем про остановку процессов."""
    any_running = any(
        _read_pid(n) and _pid_alive(_read_pid(n)) for n in ("web", "caddy", "telegram")
    )
    if not any_running:
        return
    if not sys.stdin.isatty():
        return  # в скриптах/pipe процессы не трогаем
    picked = _select(["✅ Да, остановить всё", "❌ Нет, оставить работать"],
                     "Остановить все процессы (web/Caddy/Telegram)?")
    if picked and picked.startswith("✅"):
        cmd_stop_all()


# =====================================================================
# Меню (интерактивный режим с «кнопками»)
# =====================================================================

def _clear_screen():
    """Очистка экрана (только для живого терминала)."""
    if not sys.stdin.isatty():
        return
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def _ask(prompt, default=None):
    """Строка ввода с подсказкой. EOF → None, пустой ввод → default."""
    try:
        value = input(f"{prompt}: ").strip()
    except EOFError:
        return None
    return value if value else default


def _pause():
    """Пауза до нажатия Enter (чтобы вывод не улетал со экрана)."""
    try:
        input("\nНажмите Enter, чтобы вернуться в меню...")
    except EOFError:
        pass


def _pick(options, title, allow_back=True):
    """Нумерованный выбор из списка строк (запасной режим для скриптов/pipe)."""
    while True:
        print(title)
        for i, option in enumerate(options, 1):
            print(f"  {i}. {option}")
        if allow_back:
            print("  0. ← Назад")
        choice = _ask("Выберите пункт")
        if choice is None or choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        for option in options:
            if choice.lower() == option.lower():
                return option
        print("  ❌ Неверный пункт, попробуйте ещё раз")


# =====================================================================
# Клавиатурный выбор стрелками (как в файловом менеджере)
# =====================================================================

def _enable_vt():
    """Включение ANSI-escape в Windows-консоли (подсветка и перерисовка списка)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _key_getch():
    """Одно нажатие клавиши → 'up'/'down'/'home'/'end'/'pgup'/'pgdn'/'enter'/'esc' или символ.
    Windows — msvcrt, POSIX — termios (raw)."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right",
                    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn"}.get(code, "")
        if ch == "\r":
            return "enter"
        if ch == "\x1b":
            return "esc"
        return ch
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            if seq == "[C":
                return "right"
            if seq == "[D":
                return "left"
            if seq in ("[H", "[1~"):
                return "home"
            if seq in ("[F", "[4~"):
                return "end"
            if seq == "[5~":
                return "pgup"
            if seq == "[6~":
                return "pgdn"
            return "esc"
        if ch == "\r":
            return "enter"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _select(options, title, selected=0):
    """Выбор из списка клавиатурой, как в файловом менеджере:
    ↑/↓ (или W/S) — перемещение, Home/End, Enter — выбор, Esc (или Q) — отмена.
    Если stdin — не терминал (скрипт/pipe), используется нумерованный ввод _pick()."""
    if not sys.stdin.isatty():
        return _pick(options, title)
    if not options:
        print(f"  {title}: пустой список")
        return None
    _enable_vt()
    n = len(options)
    idx = max(0, min(int(selected), n - 1))
    visible = min(n, 12)
    offset = min(max(0, idx - visible + 1), max(0, n - visible))

    print(title)
    print("  ↑/↓ — выбор · Enter — подтвердить · Esc — отмена")
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()

    def _draw():
        sys.stdout.write(f"\x1b[{visible}A")  # курсор к началу списка
        for i in range(offset, offset + visible):
            opt = str(options[i])
            if len(opt) > 76:
                opt = opt[:73] + "…"
            sys.stdout.write("\x1b[2K")
            if i == idx:
                sys.stdout.write(f"\x1b[7m  › {opt}\x1b[0m\n")
            else:
                sys.stdout.write(f"    {opt}\n")
        sys.stdout.flush()

    _draw()
    try:
        while True:
            key = _key_getch()
            if key in ("up", "w", "W", "ц", "Ц"):
                if idx > 0:
                    idx -= 1
                    if idx < offset:
                        offset = max(0, offset - 1)
                    _draw()
            elif key in ("down", "s", "S", "ы", "Ы"):
                if idx < n - 1:
                    idx += 1
                    if idx >= offset + visible:
                        offset = min(max(0, n - visible), idx - visible + 1)
                    _draw()
            elif key == "home":
                idx, offset = 0, 0
                _draw()
            elif key == "end":
                idx = n - 1
                offset = max(0, n - visible)
                _draw()
            elif key == "pgup":
                idx = max(0, idx - visible)
                offset = max(0, offset - visible)
                _draw()
            elif key == "pgdn":
                idx = min(n - 1, idx + visible)
                offset = min(max(0, n - visible), offset + visible)
                _draw()
            elif key in ("enter", "\r"):
                return options[idx]
            elif key in ("esc", "q", "Q", "й", "Й"):
                return None
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
    return None


def _pick_web_mode():
    labels = [label for _, label in _WEB_MODES]
    picked = _select(labels, "Режим запуска веб-интерфейса:")
    if picked is None:
        return None
    return next(mode for mode, label in _WEB_MODES if label == picked)


def _pick_setting_key():
    """Выбор ключа настройки стрелками (список всех ключей по группам)."""
    keys = [k for _, ks in SETTINGS_GROUPS for k in ks]
    _clear_screen()
    return _select(keys, "Какую настройку изменить?")


def _ask_model_manual():
    """Ручной ввод модели (для OpenRouter и нестандартных)."""
    _clear_screen()
    print("Введите модель вручную (например: openai/gpt-4o, anthropic/claude-3.5-sonnet):")
    value = _ask("Модель")
    if not value:
        print("  (отменено)")
        return None
    return value.strip()


def _pick_value(key):
    """Выбор/ввод значения настройки: где можно — клавиатурой из списка (как в файловом
    менеджере), иначе — ручной ввод. Возвращает нормализованное значение или None."""
    _clear_screen()
    settings = load_settings()
    if key == "llm_provider":
        providers = list(get_providers())
        current = settings.get("llm_provider")
        start = providers.index(current) if current in providers else 0
        return _select(providers, "Провайдер LLM:", selected=start)
    if key == "llm_model":
        provider = settings.get("llm_provider", "DashScope")
        models = list(get_providers().get(provider, {}).get("models", []))
        current = settings.get("llm_model")
        if current and current not in models:
            models.insert(0, current)
        models.append("✍️  Ввести вручную...")
        picked = _select(models, f"Модель LLM ({provider}):")
        if picked is None:
            return None
        if picked == "✍️  Ввести вручную...":
            return _ask_model_manual()
        return picked
    if key == "ocr_model":
        models = list(_OCR_MODELS)
        current = settings.get("ocr_model")
        if current and current not in models:
            models.insert(0, current)
        return _select(models, "OCR-модель:")
    if key in _BOOL_KEYS:
        current = bool(settings.get(key, False))
        picked = _select(["true", "false"], f"{key}:", selected=0 if current else 1)
        if picked is None:
            return None
        return picked == "true"
    # Числовые и текстовые настройки — ручной ввод (с подсказкой)
    hint = _KEY_HINTS.get(key)
    print(f"Изменение настройки: {key}")
    if hint:
        print(f"  ({hint})")
    value = _ask("Значение (Enter — отмена)")
    if value is None:
        print("  (отменено)")
        return None
    normalized, error = validate_setting(key, value)
    if error:
        print(f"  ❌ {key}: {error}")
        _pause()
        return None
    return normalized


def _pick_user():
    """Выбор пользователя из списка БД стрелками. Возвращает dict пользователя или None."""
    try:
        from auth_db import get_all_users
        users = get_all_users() or []
    except Exception as e:
        print(f"  ❌ Не удалось получить список пользователей: {e}")
        _pause()
        return None
    if not users:
        print("  Пользователи не найдены (или БД недоступна)")
        _pause()
        return None
    labels = [f"{u['username']} (ID {u['id']})" for u in users]
    picked = _select(labels, "Пользователь:")
    if picked is None:
        return None
    return users[labels.index(picked)]


def _menu_setting_change():
    key = _pick_setting_key()
    if key is None:
        return
    normalized = _pick_value(key)
    if normalized is None:
        return
    target_key = apply_key_routing(key, normalized)
    # Защита от случайной перезаписи секретов (ключи API, токен Telegram)
    if target_key in _SECRET_KEYS:
        _clear_screen()
        print(f"  Текущее значение {target_key}: {mask_key(load_settings().get(target_key, ''))}")
        confirm = _select(["✅ Да, перезаписать", "❌ Нет, отмена"], "Перезаписать ключ?")
        if confirm is None or not confirm.startswith("✅"):
            print("  (отменено — ключ не тронут)")
            _pause()
            return
    if save_settings({target_key: normalized}):
        display = mask_key(normalized) if target_key in _SECRET_KEYS else normalized
        print(f"  ✅ {key} = {display}")
        apply = _select(["✅ Да", "❌ Нет"], "Применить к загруженной RAG-системе?", selected=1)
        if apply and apply.startswith("✅"):
            _apply_settings_to_rag_if_loaded()
    _pause()


def _menu_setting_get():
    key = _pick_setting_key()
    if key is None:
        return
    _clear_screen()
    settings = load_settings()
    if key in settings:
        display = mask_key(settings[key]) if key in _SECRET_KEYS else settings[key]
        print(f"{key} = {display}")
    elif key in _DEFAULTS:
        print(f"{key} = (не задан; по умолчанию: {_DEFAULTS[key]})")
    _pause()


def _menu_setting_reset():
    key = _pick_setting_key()
    if key is None:
        return
    if key not in _DEFAULTS:
        print(f"  ❌ Нет значения по умолчанию для ключа: {key}")
        _pause()
        return
    if save_settings({key: _DEFAULTS[key]}):
        print(f"  ✅ {key} сброшен к значению по умолчанию: {_DEFAULTS[key]}")
    _pause()


def _menu_settings():
    options = ["📊 Показать все настройки",
               "✏️  Изменить настройку",
               "🔍 Показать значение настройки",
               "↩️  Сбросить настройку к умолчанию",
               "← Назад"]
    while True:
        _clear_screen()
        picked = _select(options, "⚙️  НАСТРОЙКИ")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            run_command(SimpleNamespace(command="settings", action="show", full=False))
            _pause()
        elif i == 1:
            _menu_setting_change()
        elif i == 2:
            _menu_setting_get()
        elif i == 3:
            _menu_setting_reset()


def _menu_web():
    options = ["▶️  Запустить",
               "⏹  Остановить",
               "🔄 Перезапустить",
               "📋 Статус",
               "← Назад"]
    while True:
        _clear_screen()
        web_state = "🟢 запущен" if is_web_server_running() else "⚪ не запущен"
        caddy_pid = _read_pid("caddy")
        caddy_state = f"🟢 PID {caddy_pid}" if (caddy_pid and _pid_alive(caddy_pid)) else "⚪ не запущен"
        print(f"  Веб: {web_state} | Caddy: {caddy_state}")
        print()
        picked = _select(options, "🌐 ВЕБ-ИНТЕРФЕЙС")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            mode = _pick_web_mode()
            if mode is not None:
                cmd_web_start(mode)
                _pause()
        elif i == 1:
            cmd_web_stop()
            _pause()
        elif i == 2:
            mode = _pick_web_mode()
            if mode is not None:
                cmd_web_restart(mode)
                _pause()
        elif i == 3:
            cmd_web_status()
            _pause()


def _menu_rag():
    options = ["📊 Информация о базе знаний",
               "🔄 Перезагрузить базу знаний",
               "← Назад"]
    while True:
        _clear_screen()
        picked = _select(options, "🧠 БАЗА ЗНАНИЙ (RAG)")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            cmd_info()
            _pause()
        elif i == 1:
            cmd_rag_restart()
            _pause()


def _menu_telegram():
    options = ["▶️  Запустить бота",
               "⏹  Остановить бота",
               "📋 Статус",
               "← Назад"]
    while True:
        _clear_screen()
        tg_pid = _read_pid("telegram")
        tg_state = f"🟢 PID {tg_pid}" if (tg_pid and _pid_alive(tg_pid)) else "⚪ не запущен"
        settings = load_settings()
        token = settings.get("telegram_bot_token") or ""
        token_state = "задан" if (token and len(token) >= 20) else "не задан"
        print(f"  Бот: {tg_state} | Токен: {token_state}")
        print()
        picked = _select(options, "📨 TELEGRAM-БОТ")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            cmd_telegram_start()
            _pause()
        elif i == 1:
            cmd_telegram_stop()
            _pause()
        elif i == 2:
            cmd_telegram_status()
            _pause()


def _menu_users_prompt():
    options = ["👁  Показать промт",
               "✏️  Установить промт",
               "↩️  Сбросить к стандартному",
               "← Назад"]
    while True:
        _clear_screen()
        picked = _select(options, "📝 ПЕРСОНАЛЬНЫЙ ПРОМТ ПОЛЬЗОВАТЕЛЯ")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            user = _pick_user()
            if user is None:
                continue
            _clear_screen()
            show_default = _ask("Показать стандартный промт? (y/N)", default="n")
            cmd_users_prompt_get(
                user["username"],
                show_default=bool(show_default and show_default.lower() in ("y", "yes", "д", "да")),
            )
            _pause()
        elif i == 1:
            user = _pick_user()
            if user is None:
                continue
            method = _select(["Ввести текст", "Указать файл (UTF-8)"], "Как задать промт?")
            if method is None:
                continue
            if method == "Ввести текст":
                _clear_screen()
                print("Введите текст промта (пустая строка — сброс к стандартному):")
                text = _ask("Промт")
                if text is None:
                    continue
                cmd_users_prompt_set(user["username"], text=text)
            else:
                path = _ask("Путь к файлу с промтом")
                if not path:
                    continue
                cmd_users_prompt_set(user["username"], path=path)
            _pause()
        elif i == 2:
            user = _pick_user()
            if user is None:
                continue
            cmd_users_prompt_reset(user["username"])
            _pause()


def _menu_users():
    options = ["👥 Список пользователей",
               "📝 Персональный промт",
               "← Назад"]
    while True:
        _clear_screen()
        picked = _select(options, "👥 ПОЛЬЗОВАТЕЛИ")
        if picked is None or picked == "← Назад":
            return
        i = options.index(picked)
        if i == 0:
            cmd_users_list()
            _pause()
        elif i == 1:
            _menu_users_prompt()


def menu_loop():
    """Главное меню — навигация стрелками, как в файловом менеджере."""
    options = ["📊 Статус системы",
               "⚙️  Настройки",
               "🌐 Веб-интерфейс",
               "🧠 База знаний (RAG)",
               "📨 Telegram-бот",
               "👥 Пользователи",
               "🛑 Остановить всё",
               "🚪 Выход"]
    while True:
        _clear_screen()
        settings = load_settings()
        provider = settings.get("llm_provider", "?")
        model = settings.get("llm_model", "?")
        web_state = "🟢 запущен" if is_web_server_running() else "⚪ не запущен"
        tg_pid = _read_pid("telegram")
        tg_state = "🟢 запущен" if (tg_pid and _pid_alive(tg_pid)) else "⚪ не запущен"
        print("══════════════════════════════════════════════════════")
        print("  🚀 RAG-система — панель управления (CLI)")
        print(f"  {provider} / {model} | Веб: {web_state} | Telegram: {tg_state}")
        print("══════════════════════════════════════════════════════")
        picked = _select(options, "Главное меню")
        if picked is None or picked == "🚪 Выход":
            break
        i = options.index(picked)
        if i == 0:
            cmd_status()
            _pause()
        elif i == 1:
            _menu_settings()
        elif i == 2:
            _menu_web()
        elif i == 3:
            _menu_rag()
        elif i == 4:
            _menu_telegram()
        elif i == 5:
            _menu_users()
        elif i == 6:
            cmd_stop_all()
            _pause()
    _maybe_stop_on_exit()
def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None or args.command == "menu":
        menu_loop()
        return 0
    if args.command == "shell":
        interactive_loop(parser)
        return 0
    try:
        return run_command(args)
    except KeyboardInterrupt:
        print("\n(прервано)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
