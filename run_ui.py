# run_ui.py - Стартовая панель RAG-системы (все настройки + запуск)
"""Стартовая панель с выбором LLM провайдера/модели, настройками RAG и кнопками запуска"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import subprocess
import sys
import os
import json
import webbrowser
import threading
import logging

logger = logging.getLogger(__name__)

# Маппинг провайдеров: base_url и доступные модели
PROVIDERS = {
    "DashScope": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["deepseek-v4-flash", "qwen-plus", "qwen-turbo", "qwen-max"]
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["qwen/qwen-plus", "deepseek/deepseek-chat", "openai/gpt-4o-mini"]
    }
}

# Значения по умолчанию для Caddy (хранятся в rag_settings.json)
# CADDY_PATH и CADDY_DIR читаются из self.settings["caddy_path"] / self.settings["caddy_dir"]


class LauncherUI:
    def __init__(self, root):
        self.root = root
        self.root.title("RAG-система - Стартовая панель")
        self.root.geometry("800x700")
        self.root.resizable(True, True)

        # Путь к директории проекта
        self.project_dir = os.path.dirname(os.path.abspath(__file__))

        # Хранилище запущенных процессов
        self.web_process = None
        self.caddy_process = None
        self.telegram_bot = None
        self.telegram_thread = None

        # RAG-система (ленивая инициализация)
        self.rag_system = None

        # Загрузка текущих настроек
        self.settings = self.load_settings()

        self.setup_ui()

    def load_settings(self):
        """Загрузка настроек из rag_settings.json"""
        settings_file = os.path.join(self.project_dir, "rag_settings.json")
        defaults = {
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
            "caddy_path": r"C:\Users\Petrlev\AppData\Local\Microsoft\WinGet\Packages\CaddyServer.Caddy_Microsoft.Winget.Source_8wekyb3d8bbwe\caddy.exe",
            "caddy_dir": r"C:\peter\ai_bot\automation\assistant"
        }
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # Объединяем с дефолтами (недостающие ключи)
                for key, value in defaults.items():
                    if key not in loaded:
                        loaded[key] = value
                return loaded
        except Exception:
            return defaults.copy()

    def save_settings_to_file(self):
        """Сохранение настроек в rag_settings.json"""
        settings_file = os.path.join(self.project_dir, "rag_settings.json")
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить настройки:\n{e}")
            return False

    def get_rag_system(self):
        """Ленивая инициализация RAG-системы"""
        if self.rag_system is None:
            from rag_core import get_rag_system
            self.rag_system = get_rag_system()
            # Обновляем настройки в RAG-системе из текущих
            for key, value in self.settings.items():
                self.rag_system.settings.set(key, value)
            self.rag_system._setup_client()
        return self.rag_system

    def setup_ui(self):
        # Заголовок
        title_label = tk.Label(
            self.root,
            text="🚀 RAG-система — Стартовая панель",
            font=("Arial", 16, "bold")
        )
        title_label.pack(pady=(10, 2))

        subtitle_label = tk.Label(
            self.root,
            text="Настройки • Поиск • Запуск компонентов",
            font=("Arial", 9)
        )
        subtitle_label.pack(pady=(0, 8))

        # === Notebook с вкладками настроек ===
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        self._create_main_tab()
        self._create_search_tab()
        self._create_api_tab()
        self._create_telegram_tab()
        self._create_users_tab()

        # === Блок выбора режима запуска веб-интерфейса ===
        web_mode_frame = ttk.LabelFrame(self.root, text="🌐 Режим запуска веб-интерфейса", padding=5)
        web_mode_frame.pack(fill="x", padx=10, pady=(0, 3))

        self.web_mode_var = tk.StringVar(value="domain")
        domain_radio = ttk.Radiobutton(
            web_mode_frame, text="Внешний домен (assistant.proaibro.ru) — Caddy + веб-сервер",
            variable=self.web_mode_var, value="domain"
        )
        domain_radio.pack(anchor="w", padx=5, pady=1)

        external_radio = ttk.Radiobutton(
            web_mode_frame, text="Внешний адрес (85.234.31.16:8077) — веб-сервер без Caddy",
            variable=self.web_mode_var, value="external_ip"
        )
        external_radio.pack(anchor="w", padx=5, pady=1)

        local_radio = ttk.Radiobutton(
            web_mode_frame, text="Локальный (localhost:8077) — веб-сервер без Caddy",
            variable=self.web_mode_var, value="local"
        )
        local_radio.pack(anchor="w", padx=5, pady=1)

        # Подсказка
        self.web_mode_hint = tk.Label(
            web_mode_frame, text="",
            font=("Arial", 8), fg="gray", anchor="w"
        )
        self.web_mode_hint.pack(anchor="w", padx=5, pady=(0, 2))
        self._update_web_mode_hint()

        # Привязка обновления подсказки
        self.web_mode_var.trace_add("write", lambda *_: self._update_web_mode_hint())

        # === Кнопки управления ===
        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill="x", padx=10, pady=(5, 5))

        # Левая группа: кнопки настроек
        settings_btn_frame = ttk.Frame(button_frame)
        settings_btn_frame.pack(side="left")

        self.save_btn = ttk.Button(settings_btn_frame, text="💾 Сохранить настройки", command=self.save_settings)
        self.save_btn.pack(side="left", padx=3)

        self.apply_btn = ttk.Button(settings_btn_frame, text="✅ Применить", command=self.apply_settings)
        self.apply_btn.pack(side="left", padx=3)

        # Правая группа: кнопки запуска
        launch_btn_frame = ttk.Frame(button_frame)
        launch_btn_frame.pack(side="right")

        self.web_button = tk.Button(
            launch_btn_frame,
            text="🌐 Веб-интерфейс",
            font=("Arial", 10),
            command=self.launch_web,
            bg="#4CAF50",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.web_button.pack(side="left", padx=3)

        self.restart_btn = tk.Button(
            launch_btn_frame,
            text="🔄 Перезапустить RAG",
            font=("Arial", 10),
            command=self.restart_rag,
            bg="#FF9800",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.restart_btn.pack(side="left", padx=3)

        self.stop_button = tk.Button(
            launch_btn_frame,
            text="🛑 Остановить всё",
            font=("Arial", 10),
            command=self.stop_all,
            bg="#f44336",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.stop_button.pack(side="left", padx=3)

        # Статус бар
        self.status_label = tk.Label(
            self.root,
            text=f"Готов | {self.settings.get('llm_provider', '?')} / {self.settings.get('llm_model', '?')}",
            font=("Arial", 9),
            fg="gray",
            anchor="w"
        )
        self.status_label.pack(side="bottom", fill="x", padx=10, pady=(0, 5))

    def _update_web_mode_hint(self):
        """Обновление подсказки под радио-кнопками"""
        hints = {
            "domain": "🔒 Caddy (TLS) → assistant.proaibro.ru → localhost:8077. Требуются открытые порты 80/443 на роутере.",
            "external_ip": "🌍 Прямой доступ по IP. Требуется проброс порта 8077 на роутере (192.168.0.1 → 85.234.31.16:8077).",
            "local": "💻 Доступ только с этого компьютера. Caddy не запускается."
        }
        self.web_mode_hint.config(text=hints.get(self.web_mode_var.get(), ""))

    # =====================================================================
    # Вкладка: Основные
    # =====================================================================
    def _create_main_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Основные")

        ttk.Label(frame, text="Основные настройки системы:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))

        # Отключение моделей LLM
        self.disable_llm_var = tk.BooleanVar(value=self.settings.get("disable_llm_models", False))
        disable_llm_check = ttk.Checkbutton(
            frame,
            text="Отключить LLM модели (только поиск без генерации)",
            variable=self.disable_llm_var
        )
        disable_llm_check.pack(anchor="w", padx=20, pady=5)

        # Отключение гибридного поиска
        self.disable_hybrid_search_var = tk.BooleanVar(value=self.settings.get("disable_hybrid_search", False))
        disable_hybrid_search_check = ttk.Checkbutton(
            frame,
            text="Отключить гибридный поиск (передавать всю базу знаний в модели)",
            variable=self.disable_hybrid_search_var
        )
        disable_hybrid_search_check.pack(anchor="w", padx=20, pady=5)

        # Отключение поиска в базе знаний
        self.disable_knowledge_base_search_var = tk.BooleanVar(
            value=self.settings.get("disable_knowledge_base_search", False)
        )
        disable_kb_search_check = ttk.Checkbutton(
            frame,
            text="Отключить поиск в базе знаний (работать только через LLM без Database)",
            variable=self.disable_knowledge_base_search_var
        )
        disable_kb_search_check.pack(anchor="w", padx=20, pady=5)

        # Информация о системе
        info_frame = ttk.LabelFrame(frame, text="Информация о системе")
        info_frame.pack(fill="x", padx=20, pady=20)

        self.info_label = ttk.Label(info_frame, text="RAG не инициализирована. Нажмите «Применить» для загрузки.")
        self.info_label.pack(anchor="w", padx=5, pady=5)

        # Кнопка обновления информации
        ttk.Button(info_frame, text="🔄 Обновить информацию", command=self.refresh_info).pack(anchor="w", padx=5, pady=5)

    def refresh_info(self):
        """Обновление информации о системе"""
        try:
            rag = self.get_rag_system()
            knowledge_count = len(rag.my_knowledge) if rag.my_knowledge else 0
            files_count = len(rag.all_knowledge_dict) if rag.all_knowledge_dict else 0
            self.info_label.config(
                text=f"Фрагментов в базе знаний: {knowledge_count}\n"
                     f"Загружено файлов: {files_count}"
            )
        except Exception as e:
            self.info_label.config(text=f"Ошибка: {e}")

    # =====================================================================
    # Вкладка: Поиск
    # =====================================================================
    def _create_search_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Поиск")

        ttk.Label(frame, text="Параметры поиска релевантной информации:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))

        # TOP_K
        ttk.Label(frame, text="Количество релевантных фрагментов (TOP_K):").pack(anchor="w", padx=20, pady=(5, 0))
        self.top_k_var = tk.StringVar(value=str(self.settings.get("search_top_k", 10)))
        top_k_entry = ttk.Entry(frame, textvariable=self.top_k_var, width=10)
        top_k_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(frame, text="Сколько фрагментов из базы знаний передавать в модель (1-50)", foreground="gray").pack(anchor="w", padx=20)

        # ALPHA
        ttk.Label(frame, text="Вес семантического поиска (ALPHA):").pack(anchor="w", padx=20, pady=(10, 0))
        self.alpha_var = tk.StringVar(value=str(self.settings.get("search_alpha", 0.7)))
        alpha_entry = ttk.Entry(frame, textvariable=self.alpha_var, width=10)
        alpha_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(frame, text="0.0 — только ключевые слова, 1.0 — только семантика (0.0-1.0)", foreground="gray").pack(anchor="w", padx=20)

        # Порог релевантности
        ttk.Label(frame, text="Порог релевантности:").pack(anchor="w", padx=20, pady=(10, 0))
        self.threshold_var = tk.StringVar(value=str(self.settings.get("relevance_threshold", 0.2)))
        threshold_entry = ttk.Entry(frame, textvariable=self.threshold_var, width=10)
        threshold_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(frame, text="Минимальный скор релевантности (0.0-1.0). Рекомендуется 0.1-0.3", foreground="gray").pack(anchor="w", padx=20)

        # MAX_CONTEXT_FRAGMENTS
        ttk.Label(frame, text="Максимум фрагментов при отключенном поиске:").pack(anchor="w", padx=20, pady=(10, 0))
        self.max_context_var = tk.StringVar(value=str(self.settings.get("max_context_fragments", 100)))
        max_context_entry = ttk.Entry(frame, textvariable=self.max_context_var, width=10)
        max_context_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(frame, text="Количество фрагментов при отключенном гибридном поиске (10-500)", foreground="gray").pack(anchor="w", padx=20)

    # =====================================================================
    # Вкладка: API
    # =====================================================================
    def _create_api_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="API")

        ttk.Label(frame, text="Настройки LLM API:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))

        # Провайдер
        ttk.Label(frame, text="Провайдер:").pack(anchor="w", padx=20, pady=(5, 0))
        current_provider = self.settings.get("llm_provider", "DashScope")
        self.provider_var = tk.StringVar(value=current_provider)
        self.provider_combo = ttk.Combobox(
            frame, textvariable=self.provider_var,
            values=list(PROVIDERS.keys()), state="readonly", width=47
        )
        self.provider_combo.pack(fill="x", padx=20, pady=5)
        self.provider_combo.bind("<<ComboboxSelected>>", self.on_provider_change)

        # Модель
        ttk.Label(frame, text="Модель:").pack(anchor="w", padx=20, pady=(5, 0))
        provider_models = PROVIDERS.get(current_provider, PROVIDERS["DashScope"])["models"]
        self.model_var = tk.StringVar(value=self.settings.get("llm_model", provider_models[0]))
        # OpenRouter: модель вводится вручную (тысячи моделей), для остальных — выпадающий список
        model_state = "normal" if current_provider == "OpenRouter" else "readonly"
        self.model_combo = ttk.Combobox(
            frame, textvariable=self.model_var,
            values=provider_models, state=model_state, width=47
        )
        self.model_combo.pack(fill="x", padx=20, pady=5)
        if current_provider == "OpenRouter":
            ttk.Label(frame, text="Модель OpenRouter вводится вручную, например: openai/gpt-4o, anthropic/claude-3.5-sonnet", foreground="gray").pack(anchor="w", padx=20)

        # Base URL
        ttk.Label(frame, text="Base URL:").pack(anchor="w", padx=20, pady=(10, 0))
        self.base_url_var = tk.StringVar(value=self.settings.get("llm_base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"))
        base_url_entry = ttk.Entry(frame, textvariable=self.base_url_var, width=50)
        base_url_entry.pack(fill="x", padx=20, pady=5)

        # API ключ
        ttk.Label(frame, text="API ключ:").pack(anchor="w", padx=20, pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self.settings.get("llm_api_key", ""))
        api_key_entry = ttk.Entry(frame, textvariable=self.api_key_var, width=50, show="*")
        api_key_entry.pack(fill="x", padx=20, pady=5)
        ttk.Label(frame, text="Ключ API для доступа к LLM провайдеру", foreground="gray").pack(anchor="w", padx=20)

    def on_provider_change(self, event=None):
        """Обновление списка моделей при смене провайдера"""
        provider = self.provider_var.get()
        models = PROVIDERS.get(provider, PROVIDERS["DashScope"])["models"]
        self.model_combo['values'] = models
        if provider == "OpenRouter":
            # Ручной ввод: оставляем текущую модель, не сбрасываем на первую из списка
            self.model_combo.config(state="normal")
            self.model_combo.set(self.settings.get("llm_model", ""))
        else:
            self.model_combo.config(state="readonly")
            self.model_combo.set(models[0])
        # Автоматически подставляем base_url
        self.base_url_var.set(PROVIDERS.get(provider, PROVIDERS["DashScope"])["base_url"])
        # Подставляем ключ соответствующего провайдера в поле API-ключа
        if provider == "DeepSeek":
            self.api_key_var.set(self.settings.get("llm_provider_api_key", ""))
        elif provider == "OpenRouter":
            self.api_key_var.set(self.settings.get("llm_openrouter_api_key", ""))
        else:
            self.api_key_var.set(self.settings.get("llm_api_key", ""))

    # =====================================================================
    # Вкладка: Telegram
    # =====================================================================
    def _create_telegram_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Telegram")

        ttk.Label(frame, text="Настройки Telegram бота:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))

        # Telegram Bot Token
        ttk.Label(frame, text="Telegram Bot Token:").pack(anchor="w", padx=20, pady=(5, 0))
        self.telegram_token_var = tk.StringVar(value=self.settings.get("telegram_bot_token", ""))
        telegram_token_entry = ttk.Entry(frame, textvariable=self.telegram_token_var, width=50, show="*")
        telegram_token_entry.pack(fill="x", padx=20, pady=5)
        ttk.Label(frame, text="Токен вашего Telegram бота от @BotFather", foreground="gray").pack(anchor="w", padx=20)

        # Инструкция
        instruction_frame = ttk.LabelFrame(frame, text="Инструкция")
        instruction_frame.pack(fill="x", padx=20, pady=10)
        instruction_text = (
            "1. Найдите @BotFather в Telegram\n"
            "2. Отправьте команду /newbot\n"
            "3. Следуйте инструкциям для создания бота\n"
            "4. Скопируйте токен и вставьте в поле выше\n"
            "5. Нажмите «Сохранить настройки» для сохранения"
        )
        ttk.Label(instruction_frame, text=instruction_text, justify="left").pack(anchor="w", padx=5, pady=5)

        # Статус и кнопки управления ботом
        self.telegram_status_var = tk.StringVar(value="Бот не запущен")
        self.telegram_status_label = ttk.Label(frame, textvariable=self.telegram_status_var, font=("Arial", 10, "bold"))
        self.telegram_status_label.pack(anchor="w", padx=20, pady=(10, 5))

        tg_btn_frame = ttk.Frame(frame)
        tg_btn_frame.pack(fill="x", padx=20, pady=10)

        self.start_telegram_button = ttk.Button(tg_btn_frame, text="▶️ Запустить бота", command=self.start_telegram_bot)
        self.start_telegram_button.pack(side="left", padx=5)

        self.stop_telegram_button = ttk.Button(tg_btn_frame, text="⏹ Остановить бота", command=self.stop_telegram_bot, state="disabled")
        self.stop_telegram_button.pack(side="left", padx=5)

    def start_telegram_bot(self):
        """Запуск Telegram бота"""
        try:
            token = self.telegram_token_var.get().strip()
            if not token or len(token) < 20:
                messagebox.showerror("Ошибка", "Пожалуйста, введите валидный Telegram Bot Token")
                return

            # Сохраняем токен в настройки
            self.settings["telegram_bot_token"] = token
            self.save_settings_to_file()

            from telegram_bot import TelegramRAGBot
            self.telegram_bot = TelegramRAGBot(token)

            self.telegram_thread = threading.Thread(target=self._run_telegram_bot, daemon=True)
            self.telegram_thread.start()

            self.telegram_status_var.set("✅ Бот запущен")
            self.start_telegram_button.config(state="disabled")
            self.stop_telegram_button.config(state="normal")
            messagebox.showinfo("Telegram бот", "Бот успешно запущен! Проверьте Telegram.")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка запуска Telegram бота: {e}")

    def _run_telegram_bot(self):
        """Внутренний метод для запуска бота"""
        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.telegram_bot.run()
        except Exception as e:
            logger.error(f"Ошибка в работе Telegram бота: {e}", exc_info=True)
            self.root.after(0, lambda e=e: messagebox.showerror("Ошибка", f"Ошибка в работе Telegram бота: {e}"))

    def stop_telegram_bot(self):
        """Остановка Telegram бота"""
        try:
            if hasattr(self, 'telegram_bot') and self.telegram_bot:
                self.telegram_bot.stop()
                self.telegram_status_var.set("⏹ Бот остановлен")
                self.start_telegram_button.config(state="normal")
                self.stop_telegram_button.config(state="disabled")
                messagebox.showinfo("Telegram бот", "Бот остановлен")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка остановки Telegram бота: {e}")

    

    # =====================================================================
    # Вкладка: Пользователи
    # =====================================================================
    def _create_users_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Пользователи")

        ttk.Label(frame, text="Зарегистрированные пользователи:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 5))

        # Таблица пользователей
        columns = ("id", "username", "folder", "login", "password")
        self.users_tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        self.users_tree.heading("id", text="ID")
        self.users_tree.heading("username", text="Имя пользователя")
        self.users_tree.heading("folder", text="Папка в Database")
        self.users_tree.heading("login", text="Логин")
        self.users_tree.heading("password", text="Пароль")
        self.users_tree.column("id", width=50, anchor="center")
        self.users_tree.column("username", width=180, anchor="w")
        self.users_tree.column("folder", width=190, anchor="w")
        self.users_tree.column("login", width=150, anchor="w")
        self.users_tree.column("password", width=230, anchor="w")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.users_tree.yview)
        self.users_tree.configure(yscrollcommand=scrollbar.set)
        self.users_tree.pack(fill="both", expand=True, padx=20, pady=5)
        scrollbar.pack(side="right", fill="y")

        hint = ("Пароли хранятся в базе только в виде хэша (werkzeug) и не могут быть показаны или восстановлены. "
                "Логин совпадает с именем пользователя. ✅ — папка базы знаний существует на диске.")
        ttk.Label(frame, text=hint, foreground="gray", wraplength=720, justify="left").pack(anchor="w", padx=20, pady=(5, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=20, pady=10)
        ttk.Button(btn_frame, text="🔄 Обновить список", command=self.refresh_users).pack(side="left")
        self.users_status_var = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self.users_status_var, foreground="gray").pack(side="left", padx=10)

        self.refresh_users()

    def refresh_users(self):
        """Загрузка списка пользователей из PostgreSQL (локальная БД)"""
        # Очищаем таблицу
        for item in self.users_tree.get_children():
            self.users_tree.delete(item)
        try:
            from auth_db import get_all_users
            users = get_all_users()
            if not users:
                self.users_status_var.set("Пользователи не найдены (или БД недоступна)")
                return
            for u in users:
                user_id = u["id"]
                folder = f"user_{user_id}"
                folder_path = os.path.join(self.project_dir, "Database", folder)
                folder_display = folder + ("  ✅" if os.path.isdir(folder_path) else "  (нет папки)")
                self.users_tree.insert("", "end", values=(
                    user_id, u["username"], folder_display, u["username"], "••• (хэш)"
                ))
            self.users_status_var.set(f"Всего пользователей: {len(users)}")
        except Exception as e:
            self.users_status_var.set(f"Ошибка загрузки: {e}")

    # =====================================================================
    # Валидация и сохранение настроек
    # =====================================================================
    def validate_and_apply_settings(self):
        """Валидация и применение настроек"""
        # Валидация TOP_K
        try:
            top_k = int(self.top_k_var.get())
            if 1 <= top_k <= 50:
                self.settings["search_top_k"] = top_k
            else:
                messagebox.showwarning("Предупреждение", "TOP_K должен быть от 1 до 50. Установлено 10.")
                self.settings["search_top_k"] = 10
                self.top_k_var.set("10")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение TOP_K. Установлено 10.")
            self.settings["search_top_k"] = 10
            self.top_k_var.set("10")

        # Валидация ALPHA
        try:
            alpha = float(self.alpha_var.get())
            if 0.0 <= alpha <= 1.0:
                self.settings["search_alpha"] = alpha
            else:
                messagebox.showwarning("Предупреждение", "ALPHA должен быть от 0.0 до 1.0. Установлено 0.7.")
                self.settings["search_alpha"] = 0.7
                self.alpha_var.set("0.7")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение ALPHA. Установлено 0.7.")
            self.settings["search_alpha"] = 0.7
            self.alpha_var.set("0.7")

        # Валидация порога релевантности
        try:
            threshold = float(self.threshold_var.get())
            if 0.0 <= threshold <= 1.0:
                self.settings["relevance_threshold"] = threshold
            else:
                messagebox.showwarning("Предупреждение", "Порог релевантности должен быть от 0.0 до 1.0. Установлено 0.2.")
                self.settings["relevance_threshold"] = 0.2
                self.threshold_var.set("0.2")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение порога релевантности. Установлено 0.2.")
            self.settings["relevance_threshold"] = 0.2
            self.threshold_var.set("0.2")

        # Валидация MAX_CONTEXT_FRAGMENTS
        try:
            max_context = int(self.max_context_var.get())
            if 10 <= max_context <= 500:
                self.settings["max_context_fragments"] = max_context
            else:
                messagebox.showwarning("Предупреждение", "Максимум фрагментов должен быть от 10 до 500. Установлено 100.")
                self.settings["max_context_fragments"] = 100
                self.max_context_var.set("100")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение максимума фрагментов. Установлено 100.")
            self.settings["max_context_fragments"] = 100
            self.max_context_var.set("100")

        # Основные настройки
        self.settings["disable_llm_models"] = self.disable_llm_var.get()
        self.settings["disable_hybrid_search"] = self.disable_hybrid_search_var.get()
        self.settings["disable_knowledge_base_search"] = self.disable_knowledge_base_search_var.get()

        # API настройки
        self.settings["llm_provider"] = self.provider_var.get().strip()
        self.settings["llm_model"] = self.model_var.get().strip()
        self.settings["llm_base_url"] = self.base_url_var.get().strip()
        # Ключ сохраняем в поле, соответствующее провайдеру (DeepSeek → llm_provider_api_key,
        # OpenRouter → llm_openrouter_api_key, остальные → llm_api_key, который также используется для OCR DashScope)
        api_key = self.api_key_var.get().strip()
        if self.settings["llm_provider"] == "DeepSeek":
            self.settings["llm_provider_api_key"] = api_key
            if not self.settings.get("llm_api_key"):
                self.settings["llm_api_key"] = api_key
        elif self.settings["llm_provider"] == "OpenRouter":
            self.settings["llm_openrouter_api_key"] = api_key
            if not self.settings.get("llm_api_key"):
                self.settings["llm_api_key"] = api_key
        else:
            self.settings["llm_api_key"] = api_key

        # Telegram настройки
        self.settings["telegram_bot_token"] = self.telegram_token_var.get().strip()

        return True

    def save_settings(self):
        """Сохранить настройки (валидация + запись в файл)"""
        try:
            if self.validate_and_apply_settings():
                if self.save_settings_to_file():
                    messagebox.showinfo("Настройки", "Настройки сохранены!")
                    self.status_label.config(
                        text=f"✅ Настройки сохранены | {self.settings['llm_provider']} / {self.settings['llm_model']}",
                        fg="green"
                    )
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка сохранения настроек: {e}")

    def apply_settings(self):
        """Применить настройки (сохранить + обновить RAG-систему)"""
        try:
            if self.validate_and_apply_settings():
                if self.save_settings_to_file():
                    # Обновляем настройки в RAG-системе, если она уже инициализирована
                    if self.rag_system is not None:
                        for key, value in self.settings.items():
                            self.rag_system.settings.set(key, value)
                        self.rag_system._setup_client()
                        self.refresh_info()
                    messagebox.showinfo("Настройки", "Настройки применены!")
                    self.status_label.config(
                        text=f"✅ Настройки применены | {self.settings['llm_provider']} / {self.settings['llm_model']}",
                        fg="green"
                    )
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка применения настроек: {e}")

    # =====================================================================
    # Запуск компонентов
    # =====================================================================
    def start_caddy(self):
        """Запуск Caddy reverse proxy"""
        if self.caddy_process and self.caddy_process.poll() is None:
            return
        try:
            self.caddy_process = subprocess.Popen(
                [self.settings["caddy_path"], "run"],
                cwd=self.settings["caddy_dir"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            messagebox.showwarning("Предупреждение", f"Не удалось запустить Caddy:\n{e}")

    def stop_caddy(self):
        """Остановка Caddy"""
        if self.caddy_process and self.caddy_process.poll() is None:
            self.caddy_process.terminate()
            try:
                self.caddy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.caddy_process.kill()
            self.caddy_process = None

    def _resolve_web_python(self):
        """Возвращает (python, env) для запуска run_web.py (кроссплатформенно).
        Windows: venv/Scripts/python.exe — uv-обёртка, порождает дочерний uv python с
        непредсказуемым окружением, поэтому запускаем uv python напрямую с явным
        PYTHONPATH на site-packages venv проекта.
        Linux: venv/bin/python — обычный venv, используем его напрямую."""
        env = dict(os.environ)

        if os.name == 'nt':
            venv_sp = os.path.join(self.project_dir, 'venv', 'Lib', 'site-packages')
            hermes_sp = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'hermes',
                                     'hermes-agent', 'venv', 'Lib', 'site-packages')
            paths = [venv_sp]
            if os.path.isdir(hermes_sp):
                paths.append(hermes_sp)  # там лежит fitz (PyMuPDF)
            env['PYTHONPATH'] = os.pathsep.join(paths)
            # Ищем uv python (он точно есть, т.к. venv создан через uv)
            uv_python = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'uv', 'python',
                                     'cpython-3.11-windows-x86_64-none', 'python.exe')
            if os.path.exists(uv_python):
                return uv_python, env
            return sys.executable, env
        else:
            # Linux/Ubuntu: обычный venv python
            venv_python = os.path.join(self.project_dir, 'venv', 'bin', 'python')
            if os.path.exists(venv_python):
                return venv_python, env
            return sys.executable, env

    def launch_web(self):
        """Запуск веб-интерфейса в выбранном режиме"""
        if self.web_process and self.web_process.poll() is None:
            messagebox.showinfo("Информация", "Веб-интерфейс уже запущен!")
            return

        mode = self.web_mode_var.get()
        domain_url = "https://assistant.proaibro.ru"
        external_url = "http://85.234.31.16:8077"
        local_url = "http://localhost:8077"

        try:
            # Сначала сохраняем настройки
            self.validate_and_apply_settings()
            self.save_settings_to_file()

            # Запускаем Caddy только для режима "домен"
            if mode == "domain":
                self.start_caddy()

            # Запускаем веб-сервер (uv python с явным PYTHONPATH на venv site-packages)
            python_exe, web_env = self._resolve_web_python()
            script_path = os.path.join(self.project_dir, "run_web.py")

            self.web_process = subprocess.Popen(
                [python_exe, script_path],
                cwd=self.project_dir,
                env=web_env
            )

            # Определяем URL для открытия
            if mode == "domain":
                open_url = domain_url
                mode_label = "Внешний домен"
            elif mode == "external_ip":
                open_url = external_url
                mode_label = "Внешний IP"
            else:
                open_url = local_url
                mode_label = "Локальный"

            self.status_label.config(
                text=f"✅ Веб запущен: {mode_label} | {self.settings['llm_provider']} / {self.settings['llm_model']}",
                fg="green"
            )

            def open_browser():
                import time
                time.sleep(2)
                webbrowser.open(open_url)

            threading.Thread(target=open_browser, daemon=True).start()

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить веб-интерфейс:\n{str(e)}")
            self.status_label.config(text="❌ Ошибка запуска", fg="red")

    def restart_rag(self):
        """Перезапуск RAG-системы (перезагрузка базы знаний)"""
        try:
            self.status_label.config(text="🔄 Перезагрузка RAG...", fg="orange")
            self.root.update()
            rag = self.get_rag_system()
            rag.reload_knowledge_base()
            self.refresh_info()
            self.status_label.config(
                text=f"✅ RAG перезагружена | {self.settings['llm_provider']} / {self.settings['llm_model']}",
                fg="green"
            )
            messagebox.showinfo("RAG", "База знаний перезагружена!")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка перезагрузки RAG: {e}")
            self.status_label.config(text="❌ Ошибка перезагрузки RAG", fg="red")

    def stop_all(self):
        """Остановка всех запущенных процессов"""
        stopped = []

        if self.web_process and self.web_process.poll() is None:
            self.web_process.terminate()
            try:
                self.web_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.web_process.kill()
            self.web_process = None
            stopped.append("Веб-интерфейс")

        if self.caddy_process and self.caddy_process.poll() is None:
            self.stop_caddy()
            stopped.append("Caddy")

        if hasattr(self, 'telegram_bot') and self.telegram_bot:
            try:
                self.telegram_bot.stop()
                self.telegram_status_var.set("⏹ Бот остановлен")
                self.start_telegram_button.config(state="normal")
                self.stop_telegram_button.config(state="disabled")
            except Exception:
                pass
            stopped.append("Telegram бот")

        if stopped:
            self.status_label.config(text=f"🛑 Остановлено: {', '.join(stopped)}", fg="red")
        else:
            self.status_label.config(text="Ничего не запущено", fg="gray")

    def on_closing(self):
        """Обработка закрытия окна"""
        self.stop_all()
        self.root.destroy()


def main():
    """Запуск стартовой панели"""
    root = tk.Tk()
    app = LauncherUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()