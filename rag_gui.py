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
        
        # Новая вкладка Чат
        chat_frame = ttk.Frame(notebook)
        notebook.add(chat_frame, text="Чат")
        
        # === Основные настройки ===
        ttk.Label(main_frame, text="Основные настройки системы:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))
        
        # Отключение моделей OpenRouter
        self.disable_openrouter_var = tk.BooleanVar(value=self.settings.get("disable_openrouter_models", False))
        disable_openrouter_check = ttk.Checkbutton(
            main_frame, 
            text="Отключить модели OpenRouter (только поиск без генерации)", 
            variable=self.disable_openrouter_var
        )
        disable_openrouter_check.pack(anchor="w", padx=20, pady=5)
        
        # Отключение гибридного поиска
        self.disable_hybrid_search_var = tk.BooleanVar(value=self.settings.get("disable_hybrid_search", False))
        disable_hybrid_search_check = ttk.Checkbutton(
            main_frame, 
            text="Отключить гибридный поиск (передавать всю базу знаний в модели)", 
            variable=self.disable_hybrid_search_var
        )
        disable_hybrid_search_check.pack(anchor="w", padx=20, pady=5)
        
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
        ttk.Label(api_frame, text="Настройки OpenRouter API:", font=("Arial", 12, "bold")).pack(anchor="w", pady=(10, 10))
        
        # Базовый URL
        ttk.Label(api_frame, text="Base URL:").pack(anchor="w", padx=20, pady=(5, 0))
        self.base_url_var = tk.StringVar(value=self.settings.get("openrouter_base_url", "https://openrouter.ai/api/v1"))
        base_url_entry = ttk.Entry(api_frame, textvariable=self.base_url_var, width=50)
        base_url_entry.pack(fill="x", padx=20, pady=5)
        
        # API ключ
        ttk.Label(api_frame, text="API ключ:").pack(anchor="w", padx=20, pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self.settings.get("openrouter_api_key", ""))
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, width=50, show="*")
        api_key_entry.pack(fill="x", padx=20, pady=5)
        
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
        self.settings.set("disable_openrouter_models", self.disable_openrouter_var.get())
        self.settings.set("disable_hybrid_search", self.disable_hybrid_search_var.get())
        
        # Применение API настроек
        self.settings.set("openrouter_base_url", self.base_url_var.get().strip())
        self.settings.set("openrouter_api_key", self.api_key_var.get().strip())
        
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