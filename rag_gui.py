# rag_gui.py - Графический интерфейс для RAG-системы
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import logging

logger = logging.getLogger(__name__)

class SettingsWindow:
    """Окно настроек RAG-системы"""
    def __init__(self, settings, rag_system):
        self.settings = settings
        self.rag_system = rag_system
        self.window = None
        self.create_window()
    
    def create_window(self):
        """Создание окна настроек"""
        self.window = tk.Tk()
        self.window.title("Настройки RAG-системы")
        self.window.geometry("800x650")
        self.window.resizable(True, True)
        
        # Создание вкладок
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Вкладка основных настроек
        main_frame = ttk.Frame(notebook)
        notebook.add(main_frame, text="Основные")
        
        # Вкладка настроек поиска
        search_frame = ttk.Frame(notebook)
        notebook.add(search_frame, text="Поиск")
        
        # Вкладка API настроек
        api_frame = ttk.Frame(notebook)
        notebook.add(api_frame, text="API")
        
        # Вкладка Telegram настроек
        telegram_frame = ttk.Frame(notebook)
        notebook.add(telegram_frame, text="Telegram")
        
        # Новая вкладка Чат
        chat_frame = ttk.Frame(notebook)
        notebook.add(chat_frame, text="Чат")
        
        # === Основные настройки ===
        ttk.Label(main_frame, text="Основные настройки системы:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))
        
        # Отключение моделей LLM
        self.disable_llm_var = tk.BooleanVar(value=self.settings.get("disable_llm_models", False))
        disable_llm_check = ttk.Checkbutton(
            main_frame, 
            text="Отключить LLM модели (только поиск без генерации)", 
            variable=self.disable_llm_var
        )
        disable_llm_check.pack(anchor="w", padx=20, pady=5)
        
        # Отключение гибридного поиска
        self.disable_hybrid_search_var = tk.BooleanVar(value=self.settings.get("disable_hybrid_search", False))
        disable_hybrid_search_check = ttk.Checkbutton(
            main_frame, 
            text="Отключить гибридный поиск (передавать всю базу знаний в модели)", 
            variable=self.disable_hybrid_search_var
        )
        disable_hybrid_search_check.pack(anchor="w", padx=20, pady=5)
        
        # Отключение поиска в базе знаний (работа только через LLM)
        self.disable_knowledge_base_search_var = tk.BooleanVar(value=self.settings.get("disable_knowledge_base_search", False))
        disable_kb_search_check = ttk.Checkbutton(
            main_frame, 
            text="Отключить поиск в базе знаний (работать только через LLM без использования Database)", 
            variable=self.disable_knowledge_base_search_var
        )
        disable_kb_search_check.pack(anchor="w", padx=20, pady=5)
        
        # Информация о текущем состоянии
        info_frame = ttk.LabelFrame(main_frame, text="Информация о системе")
        info_frame.pack(fill="x", padx=20, pady=20)
        
        ttk.Label(info_frame, text=f"Фрагментов в базе знаний: {len(self.rag_system.my_knowledge) if self.rag_system.my_knowledge else 0}").pack(anchor="w", padx=5, pady=2)
        ttk.Label(info_frame, text=f"Загружено файлов: {len(self.rag_system.all_knowledge_dict) if self.rag_system.all_knowledge_dict else 0}").pack(anchor="w", padx=5, pady=2)
        
        # === Настройки поиска ===
        ttk.Label(search_frame, text="Параметры поиска релевантной информации:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))
        
        # TOP_K - количество релевантных фрагментов
        ttk.Label(search_frame, text="Количество релевантных фрагментов (TOP_K):").pack(anchor="w", padx=20, pady=(5, 0))
        self.top_k_var = tk.StringVar(value=str(self.settings.get("search_top_k", 10)))
        top_k_entry = ttk.Entry(search_frame, textvariable=self.top_k_var, width=10)
        top_k_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(search_frame, text="Сколько фрагментов из базы знаний передавать в модель (1-50)", foreground="gray").pack(anchor="w", padx=20)
        
        # ALPHA - вес семантического поиска
        ttk.Label(search_frame, text="Вес семантического поиска (ALPHA):").pack(anchor="w", padx=20, pady=(10, 0))
        self.alpha_var = tk.StringVar(value=str(self.settings.get("search_alpha", 0.7)))
        alpha_entry = ttk.Entry(search_frame, textvariable=self.alpha_var, width=10)
        alpha_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(search_frame, text="0.0 - только ключевые слова, 1.0 - только семантика (0.0-1.0)", foreground="gray").pack(anchor="w", padx=20)
        
        # Порог релевантности
        ttk.Label(search_frame, text="Порог релевантности:").pack(anchor="w", padx=20, pady=(10, 0))
        self.threshold_var = tk.StringVar(value=str(self.settings.get("relevance_threshold", 0.2)))
        threshold_entry = ttk.Entry(search_frame, textvariable=self.threshold_var, width=10)
        threshold_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(search_frame, text="Минимальный скор релевантности (0.0-1.0). Рекомендуется 0.1-0.3", foreground="gray").pack(anchor="w", padx=20)
        
        # MAX_CONTEXT_FRAGMENTS - максимальное количество фрагментов при отключенном поиске
        ttk.Label(search_frame, text="Максимум фрагментов при отключенном поиске:").pack(anchor="w", padx=20, pady=(10, 0))
        self.max_context_var = tk.StringVar(value=str(self.settings.get("max_context_fragments", 100)))
        max_context_entry = ttk.Entry(search_frame, textvariable=self.max_context_var, width=10)
        max_context_entry.pack(anchor="w", padx=20, pady=5)
        ttk.Label(search_frame, text="Количество фрагментов при отключенном гибридном поиске (10-500)", foreground="gray").pack(anchor="w", padx=20)
        
        # === API настройки ===
        ttk.Label(api_frame, text="Настройки LLM API:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))
        
        # Провайдер
        ttk.Label(api_frame, text="Провайдер:").pack(anchor="w", padx=20, pady=(5, 0))
        self.provider_var = tk.StringVar(value=self.settings.get("llm_provider", "DashScope"))
        provider_combo = ttk.Combobox(api_frame, textvariable=self.provider_var, values=["DashScope", "OpenRouter"], state="readonly", width=47)
        provider_combo.pack(fill="x", padx=20, pady=5)
        
        # Модель
        ttk.Label(api_frame, text="Модель:").pack(anchor="w", padx=20, pady=(5, 0))
        self.model_var = tk.StringVar(value=self.settings.get("llm_model", "deepseek-v4-flash"))
        model_combo = ttk.Combobox(api_frame, textvariable=self.model_var, values=["deepseek-v4-flash", "qwen-plus", "qwen-turbo"], state="readonly", width=47)
        model_combo.pack(fill="x", padx=20, pady=5)
        
        # Базовый URL
        ttk.Label(api_frame, text="Base URL:").pack(anchor="w", padx=20, pady=(5, 0))
        self.base_url_var = tk.StringVar(value=self.settings.get("llm_base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"))
        base_url_entry = ttk.Entry(api_frame, textvariable=self.base_url_var, width=50)
        base_url_entry.pack(fill="x", padx=20, pady=5)
        
        # API ключ
        ttk.Label(api_frame, text="API ключ:").pack(anchor="w", padx=20, pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self.settings.get("llm_api_key", ""))
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, width=50, show="*")
        api_key_entry.pack(fill="x", padx=20, pady=5)
        
        # === Telegram настройки ===
        ttk.Label(telegram_frame, text="Настройки Telegram бота:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))

        # Telegram Bot Token
        ttk.Label(telegram_frame, text="Telegram Bot Token:").pack(anchor="w", padx=20, pady=(5, 0))
        self.telegram_token_var = tk.StringVar(value=self.settings.get("telegram_bot_token", ""))
        telegram_token_entry = ttk.Entry(telegram_frame, textvariable=self.telegram_token_var, width=50, show="*")
        telegram_token_entry.pack(fill="x", padx=20, pady=5)
        ttk.Label(telegram_frame, text="Токен вашего Telegram бота от @BotFather", foreground="gray").pack(anchor="w", padx=20)

        # Инструкция по получению токена
        instruction_frame = ttk.LabelFrame(telegram_frame, text="Инструкция")
        instruction_frame.pack(fill="x", padx=20, pady=20)
        instruction_text = (
            "1. Найдите @BotFather в Telegram\n"
            "2. Отправьте команду /newbot\n"
            "3. Следуйте инструкциям для создания бота\n"
            "4. Скопируйте токен и вставьте в поле выше\n"
            "5. Нажмите 'Применить' для сохранения настроек"
        )
        ttk.Label(instruction_frame, text=instruction_text, justify="left").pack(anchor="w", padx=5, pady=5)

        # Кнопка запуска бота
        self.telegram_status_var = tk.StringVar()
        self.telegram_status_var.set("Бот не запущен")
        self.telegram_status_label = ttk.Label(telegram_frame, textvariable=self.telegram_status_var)
        self.telegram_status_label.pack(anchor="w", padx=20, pady=(10, 5))

        button_frame_telegram = ttk.Frame(telegram_frame)
        button_frame_telegram.pack(fill="x", padx=20, pady=10)

        self.start_telegram_button = ttk.Button(button_frame_telegram, text="Запустить бота", command=self.start_telegram_bot)
        self.start_telegram_button.pack(side="left", padx=5)

        self.stop_telegram_button = ttk.Button(button_frame_telegram, text="Остановить бота", command=self.stop_telegram_bot, state="disabled")
        self.stop_telegram_button.pack(side="left", padx=5)
        
        # === Вкладка Чат ===
        # Фрейм для чата
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)
        
        # Фрейм для сообщений
        messages_frame = ttk.Frame(chat_frame)
        messages_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 0))
        messages_frame.columnconfigure(0, weight=1)
        messages_frame.rowconfigure(0, weight=1)
        
        # Текстовое поле для сообщений
        self.messages_text = scrolledtext.ScrolledText(messages_frame, wrap=tk.WORD, state='disabled')
        self.messages_text.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.messages_text.tag_config("user", foreground="blue")
        self.messages_text.tag_config("assistant", foreground="green")
        self.messages_text.tag_config("system", foreground="orange")
        
        # Фрейм для ввода сообщения
        input_frame = ttk.Frame(chat_frame)
        input_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        input_frame.columnconfigure(0, weight=1)
        
        # Поле ввода с поддержкой копирования/вставки
        self.question_text = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, height=4)
        self.question_text.grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=5)
        
        # Привязка клавиш для копирования/вставки
        self.question_text.bind("<Control-c>", self.copy_text)
        self.question_text.bind("<Control-v>", self.paste_text)
        self.question_text.bind("<Control-a>", self.select_all)
        self.question_text.bind("<Return>", self.send_message)  # Enter для отправки
        self.question_text.bind("<Control-Return>", self.send_message)  # Ctrl+Enter для отправки
        
        # Привязка клавиш для текстового поля сообщений (только копирование)
        self.messages_text.bind("<Control-c>", self.copy_messages)
        self.messages_text.bind("<1>", lambda event: self.messages_text.focus_set())
        
        # Кнопка отправки
        self.send_button = ttk.Button(input_frame, text="Отправить", command=self.send_message)
        self.send_button.grid(row=0, column=1, padx=(0, 5), pady=5)
        
        # Статусная строка
        self.status_var = tk.StringVar()
        self.status_var.set("Готов к работе")
        status_label = ttk.Label(chat_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_label.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        
        # Добавляем приветственное сообщение
        self.add_message("🤖 Добро пожаловать! Я информационный ассистент компании ГТИ-ОПТ.\n"
                        "Задайте ваш вопрос о водоочистном оборудовании, и я постараюсь ответить на основе нашей базы знаний.", "system")
        
        # Кнопки
        button_frame = ttk.Frame(self.window)
        button_frame.pack(fill="x", padx=10, pady=10)
        
        ttk.Button(button_frame, text="Сохранить настройки", command=self.save_settings).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Применить", command=self.apply_settings).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Отмена", command=self.window.destroy).pack(side="right", padx=5)
        
        # Центрирование окна
        self.window.update_idletasks()
        x = (self.window.winfo_screenwidth() // 2) - (self.window.winfo_width() // 2)
        y = (self.window.winfo_screenheight() // 2) - (self.window.winfo_height() // 2)
        self.window.geometry(f"+{x}+{y}")
    
    def copy_text(self, event=None):
        """Копирование текста из поля ввода"""
        try:
            self.question_text.event_generate("<<Copy>>")
        except tk.TclError:
            pass
        return "break"
    
    def paste_text(self, event=None):
        """Вставка текста в поле ввода"""
        try:
            self.question_text.event_generate("<<Paste>>")
        except tk.TclError:
            pass
        return "break"
    
    def select_all(self, event=None):
        """Выделение всего текста в поле ввода"""
        try:
            self.question_text.tag_add("sel", "1.0", "end")
        except tk.TclError:
            pass
        return "break"
    
    def copy_messages(self, event=None):
        """Копирование текста из поля сообщений"""
        try:
            self.messages_text.event_generate("<<Copy>>")
        except tk.TclError:
            pass
        return "break"
    
    def add_message(self, text, tag=None):
        """Добавление сообщения в чат"""
        if hasattr(self.messages_text, 'winfo_exists') and self.messages_text.winfo_exists():
            try:
                self.messages_text.config(state='normal')
                if tag:
                    self.messages_text.insert(tk.END, text + "\n\n", tag)
                else:
                    self.messages_text.insert(tk.END, text + "\n\n")
                self.messages_text.config(state='disabled')
                self.messages_text.see(tk.END)
            except tk.TclError:
                pass
    
    def send_message(self, event=None):
        """Обработка отправки сообщения"""
        question = self.question_text.get("1.0", tk.END).strip()
        if not question:
            return
        
        # Добавление вопроса пользователя в чат
        self.add_message(f"Вы: {question}", "user")
        self.question_text.delete("1.0", tk.END)
        
        # Обновление статуса
        self.status_var.set("Обработка запроса...")
        self.send_button.config(state="disabled")
        
        # Выполнение запроса в отдельном потоке
        def process_question():
            try:
                answer = self.rag_system.ask_model(question)
                self.add_message(f"Ассистент: {answer}", "assistant")
            except Exception as e:
                self.add_message(f"Ошибка: {e}", "error")
            finally:
                self.window.after(0, lambda: self.status_var.set("Готов к работе"))
                self.window.after(0, lambda: self.send_button.config(state="normal"))
        
        threading.Thread(target=process_question, daemon=True).start()
    
    def start_telegram_bot(self):
        """Запуск Telegram бота"""
        try:
            # Проверяем токен
            token = self.telegram_token_var.get().strip()
            if not token or token == "YOUR_BOT_TOKEN_HERE" or len(token) < 20:
                messagebox.showerror("Ошибка", "Пожалуйста, введите валидный Telegram Bot Token")
                return
                
            # Сохраняем токен
            self.settings.set("telegram_bot_token", token)
            self.settings.save_settings()
            
            # Импортируем и запускаем бота
            from telegram_bot import TelegramRAGBot
            self.telegram_bot = TelegramRAGBot(token)
            
            # Запускаем бота в отдельном потоке с правильной обработкой event loop
            self.telegram_thread = threading.Thread(target=self._run_telegram_bot, daemon=True)
            self.telegram_thread.start()
            
            self.telegram_status_var.set("Бот запущен")
            self.start_telegram_button.config(state="disabled")
            self.stop_telegram_button.config(state="normal")
            messagebox.showinfo("Telegram бот", "Бот успешно запущен! Проверьте Telegram.")
            
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка запуска Telegram бота: {e}")
            logger.error(f"Ошибка запуска Telegram бота: {e}")

    def _run_telegram_bot(self):
        """Внутренний метод для запуска бота"""
        try:
            # Создаем новый event loop для этого потока
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.telegram_bot.run()
        except Exception as e:
            logger.error(f"Ошибка в работе Telegram бота: {e}", exc_info=True)
            self.window.after(0, lambda e=e: messagebox.showerror("Ошибка", f"Ошибка в работе Telegram бота: {e}"))

    def stop_telegram_bot(self):
        """Остановка Telegram бота"""
        try:
            if hasattr(self, 'telegram_bot') and self.telegram_bot:
                # Здесь должна быть логика остановки бота
                self.telegram_bot.stop()
                self.telegram_status_var.set("Бот остановлен")
                self.start_telegram_button.config(state="normal")
                self.stop_telegram_button.config(state="disabled")
                messagebox.showinfo("Telegram бот", "Бот остановлен")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка остановки Telegram бота: {e}")
    
    def validate_and_apply_settings(self):
        """Валидация и применение настроек"""
        # Валидация и применение настроек поиска
        try:
            top_k = int(self.top_k_var.get())
            if 1 <= top_k <= 50:
                self.settings.set("search_top_k", top_k)
            else:
                messagebox.showwarning("Предупреждение", "TOP_K должен быть от 1 до 50. Установлено значение по умолчанию (10).")
                self.settings.set("search_top_k", 10)
                self.top_k_var.set("10")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение TOP_K. Установлено значение по умолчанию (10).")
            self.settings.set("search_top_k", 10)
            self.top_k_var.set("10")
        
        try:
            alpha = float(self.alpha_var.get())
            if 0.0 <= alpha <= 1.0:
                self.settings.set("search_alpha", alpha)
            else:
                messagebox.showwarning("Предупреждение", "ALPHA должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.7).")
                self.settings.set("search_alpha", 0.7)
                self.alpha_var.set("0.7")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение ALPHA. Установлено значение по умолчанию (0.7).")
            self.settings.set("search_alpha", 0.7)
            self.alpha_var.set("0.7")
        
        try:
            threshold = float(self.threshold_var.get())
            if 0.0 <= threshold <= 1.0:
                self.settings.set("relevance_threshold", threshold)
            else:
                messagebox.showwarning("Предупреждение", "Порог релевантности должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.2).")
                self.settings.set("relevance_threshold", 0.2)
                self.threshold_var.set("0.2")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение порога релевантности. Установлено значение по умолчанию (0.2).")
            self.settings.set("relevance_threshold", 0.2)
            self.threshold_var.set("0.2")
        
        try:
            max_context = int(self.max_context_var.get())
            if 10 <= max_context <= 500:
                self.settings.set("max_context_fragments", max_context)
            else:
                messagebox.showwarning("Предупреждение", "Максимум фрагментов должен быть от 10 до 500. Установлено значение по умолчанию (100).")
                self.settings.set("max_context_fragments", 100)
                self.max_context_var.set("100")
        except ValueError:
            messagebox.showwarning("Предупреждение", "Неверное значение максимума фрагментов. Установлено значение по умолчанию (100).")
            self.settings.set("max_context_fragments", 100)
            self.max_context_var.set("100")
        
        # Применение основных настроек
        self.settings.set("disable_llm_models", self.disable_llm_var.get())
        self.settings.set("disable_hybrid_search", self.disable_hybrid_search_var.get())
        self.settings.set("disable_knowledge_base_search", self.disable_knowledge_base_search_var.get())
        
        # Применение API настроек
        self.settings.set("llm_provider", self.provider_var.get().strip())
        self.settings.set("llm_model", self.model_var.get().strip())
        self.settings.set("llm_base_url", self.base_url_var.get().strip())
        self.settings.set("llm_api_key", self.api_key_var.get().strip())
        
        # Применение Telegram настроек
        self.settings.set("telegram_bot_token", self.telegram_token_var.get().strip())
        
        # Сохранение настроек
        self.settings.save_settings()
        
        return True
    
    def save_settings(self):
        """Сохранение настроек"""
        try:
            if self.validate_and_apply_settings():
                messagebox.showinfo("Настройки", "Настройки сохранены!")
                self.window.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка сохранения настроек: {e}")
    
    def apply_settings(self):
        """Применение настроек без закрытия окна"""
        try:
            if self.validate_and_apply_settings():
                # Обновляем настройки в RAG-системе
                self.rag_system.settings = self.settings
                
                # Если изменились настройки API, обновляем клиент
                self.rag_system._setup_client()
                
                messagebox.showinfo("Настройки", "Настройки применены!")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка применения настроек: {e}")
    
    def show(self):
        """Отображение окна настроек"""
        self.window.mainloop()