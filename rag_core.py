# rag_core.py - основная логика RAG без GUI
import openai
import os
import csv
from sentence_transformers import util, SentenceTransformer
from rank_bm25 import BM25Okapi
import numpy as np
import pickle
import hashlib
import torch
from pathlib import Path
import PyPDF2
import logging
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import sys

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RAGSettings:
    """Класс для управления настройками RAG-системы"""
    def __init__(self):
        self.settings_file = "rag_settings.json"
        self.default_settings = {
            "disable_openrouter_models": False,
            "disable_hybrid_search": False,
            "openrouter_api_key": "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",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            # Настройки поиска
            "search_top_k": 10,
            "search_alpha": 0.7,
            "relevance_threshold": 0.2,
            "max_context_fragments": 100
        }
        self.settings = self.load_settings()
    
    def load_settings(self):
        """Загрузка настроек из файла"""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    # Объединяем с настройками по умолчанию
                    for key, value in self.default_settings.items():
                        if key not in settings:
                            settings[key] = value
                    return settings
            else:
                return self.default_settings.copy()
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек: {e}")
            return self.default_settings.copy()
    
    def save_settings(self):
        """Сохранение настроек в файл"""
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
            logger.info("Настройки сохранены")
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")
    
    def get(self, key, default=None):
        """Получение значения настройки"""
        return self.settings.get(key, default)
    
    def set(self, key, value):
        """Установка значения настройки"""
        self.settings[key] = value

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
        self.window.geometry("600x550")
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
    
    def save_settings(self):
        """Сохранение настроек"""
        try:
            # Основные настройки
            self.settings.set("disable_openrouter_models", self.disable_openrouter_var.get())
            self.settings.set("disable_hybrid_search", self.disable_hybrid_search_var.get())
            
            # Настройки поиска с валидацией
            try:
                top_k = int(self.top_k_var.get())
                if 1 <= top_k <= 50:
                    self.settings.set("search_top_k", top_k)
                else:
                    messagebox.showwarning("Предупреждение", "TOP_K должен быть от 1 до 50. Установлено значение по умолчанию (10).")
                    self.settings.set("search_top_k", 10)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение TOP_K. Установлено значение по умолчанию (10).")
                self.settings.set("search_top_k", 10)
            
            try:
                alpha = float(self.alpha_var.get())
                if 0.0 <= alpha <= 1.0:
                    self.settings.set("search_alpha", alpha)
                else:
                    messagebox.showwarning("Предупреждение", "ALPHA должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.7).")
                    self.settings.set("search_alpha", 0.7)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение ALPHA. Установлено значение по умолчанию (0.7).")
                self.settings.set("search_alpha", 0.7)
            
            try:
                threshold = float(self.threshold_var.get())
                if 0.0 <= threshold <= 1.0:
                    self.settings.set("relevance_threshold", threshold)
                else:
                    messagebox.showwarning("Предупреждение", "Порог релевантности должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.2).")
                    self.settings.set("relevance_threshold", 0.2)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение порога релевантности. Установлено значение по умолчанию (0.2).")
                self.settings.set("relevance_threshold", 0.2)
            
            try:
                max_context = int(self.max_context_var.get())
                if 10 <= max_context <= 500:
                    self.settings.set("max_context_fragments", max_context)
                else:
                    messagebox.showwarning("Предупреждение", "Максимум фрагментов должен быть от 10 до 500. Установлено значение по умолчанию (100).")
                    self.settings.set("max_context_fragments", 100)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение максимума фрагментов. Установлено значение по умолчанию (100).")
                self.settings.set("max_context_fragments", 100)
            
            # API настройки
            self.settings.set("openrouter_base_url", self.base_url_var.get().strip())
            self.settings.set("openrouter_api_key", self.api_key_var.get().strip())
            
            self.settings.save_settings()
            messagebox.showinfo("Настройки", "Настройки сохранены!")
            self.window.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка сохранения настроек: {e}")
    
    def apply_settings(self):
        """Применение настроек без закрытия окна"""
        try:
            # Основные настройки
            self.settings.set("disable_openrouter_models", self.disable_openrouter_var.get())
            self.settings.set("disable_hybrid_search", self.disable_hybrid_search_var.get())
            
            # Настройки поиска с валидацией
            try:
                top_k = int(self.top_k_var.get())
                if 1 <= top_k <= 50:
                    self.settings.set("search_top_k", top_k)
                else:
                    messagebox.showwarning("Предупреждение", "TOP_K должен быть от 1 до 50. Установлено значение по умолчанию (10).")
                    self.settings.set("search_top_k", 10)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение TOP_K. Установлено значение по умолчанию (10).")
                self.settings.set("search_top_k", 10)
            
            try:
                alpha = float(self.alpha_var.get())
                if 0.0 <= alpha <= 1.0:
                    self.settings.set("search_alpha", alpha)
                else:
                    messagebox.showwarning("Предупреждение", "ALPHA должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.7).")
                    self.settings.set("search_alpha", 0.7)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение ALPHA. Установлено значение по умолчанию (0.7).")
                self.settings.set("search_alpha", 0.7)
            
            try:
                threshold = float(self.threshold_var.get())
                if 0.0 <= threshold <= 1.0:
                    self.settings.set("relevance_threshold", threshold)
                else:
                    messagebox.showwarning("Предупреждение", "Порог релевантности должен быть от 0.0 до 1.0. Установлено значение по умолчанию (0.2).")
                    self.settings.set("relevance_threshold", 0.2)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение порога релевантности. Установлено значение по умолчанию (0.2).")
                self.settings.set("relevance_threshold", 0.2)
            
            try:
                max_context = int(self.max_context_var.get())
                if 10 <= max_context <= 500:
                    self.settings.set("max_context_fragments", max_context)
                else:
                    messagebox.showwarning("Предупреждение", "Максимум фрагментов должен быть от 10 до 500. Установлено значение по умолчанию (100).")
                    self.settings.set("max_context_fragments", 100)
            except ValueError:
                messagebox.showwarning("Предупреждение", "Неверное значение максимума фрагментов. Установлено значение по умолчанию (100).")
                self.settings.set("max_context_fragments", 100)
            
            # API настройки
            self.settings.set("openrouter_base_url", self.base_url_var.get().strip())
            self.settings.set("openrouter_api_key", self.api_key_var.get().strip())
            
            self.settings.save_settings()
            messagebox.showinfo("Настройки", "Настройки применены!")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка применения настроек: {e}")
    
    def show(self):
        """Отображение окна настроек"""
        self.window.mainloop()

# === Настройки RAG-системы ===
settings = RAGSettings()

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")  # Получаем ключ из переменной окружения
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "mistralai/mistral-7b-instruct:free",
    "qwen/qwen3-30b-a3b-thinking-2507"
]

class RAGCore:
    def __init__(self):
        """Инициализация RAG-системы"""
        self.my_knowledge = []
        self.all_knowledge_dict = {}
        self.model = None
        self.corpus_embeddings = None
        self.bm25 = None
        self.tokenized_corpus = None
        self.settings = settings
        
        # Инициализация модели
        self._setup_model()
        
        # Загрузка базы знаний
        self.reload_knowledge_base()
        
    def _setup_model(self):
        """Инициализация модели эмбеддингов"""
        try:
            logger.info("🧠 Загрузка модели...")
            self.model = SentenceTransformer('intfloat/multilingual-e5-large')
            logger.info("✅ Модель загружена")
        except Exception as e:
            logger.error(f"❌ Не удалось загрузить модель: {e}")
            raise
    
    def load_knowledge_from_txt(self, file_paths=None):
        """Загрузка знаний из файлов"""
        all_knowledge = {}
        
        # Если файлы не указаны, загружаем из стандартных мест
        if not file_paths:
            logger.info("📂 Загрузка базы знаний из стандартных источников...")
            # Загрузка из DataBase.txt
            txt_file = "DataBase.txt"
            if os.path.exists(txt_file):
                logger.info(f"📄 Обработка файла: {txt_file}")
                txt_knowledge = []
                try:
                    with open(txt_file, "r", encoding="utf-8") as file:
                        for i, line in enumerate(file, 1):
                            line = line.strip()
                            if line:
                                txt_knowledge.append(line)
                    if txt_knowledge:
                        logger.info(f"✅ Загружено {len(txt_knowledge)} строк из {txt_file}")
                        all_knowledge[txt_file] = txt_knowledge
                    else:
                        logger.warning(f"⚠️ Файл {txt_file} пуст")
                except Exception as e:
                    logger.error(f"❌ Ошибка при чтении {txt_file}: {e}")
            else:
                logger.warning(f"⚠️ Файл {txt_file} не найден")
            
            # Загрузка из папки Database
            database_folder = "Database"
            if os.path.exists(database_folder):
                logger.info(f"📁 Обработка папки: {database_folder}")
                for file_name in os.listdir(database_folder):
                    file_path = os.path.join(database_folder, file_name)
                    self._process_file(file_path, all_knowledge)
            else:
                logger.warning(f"⚠️ Папка {database_folder} не найдена")
        else:
            # Загрузка указанных файлов
            logger.info(f"📂 Загрузка {len(file_paths)} выбранных файлов...")
            for file_path in file_paths:
                logger.info(f"📄 Обработка файла: {file_path}")
                self._process_file(file_path, all_knowledge)
        
        return all_knowledge
    
    def _process_file(self, file_path, all_knowledge):
        """Обработка одного файла"""
        try:
            if file_path.lower().endswith(".txt"):
                with open(file_path, "r", encoding="utf-8") as file:
                    lines = [line.strip() for line in file if line.strip()]
                    if lines:
                        logger.info(f"✅ Загружено {len(lines)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = lines
                    else:
                        logger.warning(f"⚠️ Файл {os.path.basename(file_path)} пуст")
                        
            elif file_path.lower().endswith(".csv"):
                with open(file_path, "r", encoding="utf-8") as csvfile:
                    reader = csv.DictReader(csvfile)
                    rows = list(reader)
                    csv_knowledge = []
                    for row in rows:
                        parts = []
                        for key, value in row.items():
                            clean_key = key.strip()
                            if clean_key.lower().startswith("unnamed"):
                                continue
                            if value and value.strip():
                                parts.append(f"{clean_key} — {value.strip()}")
                        
                        if parts:
                            if "Проект" in row and "Ответственный" in row:
                                entry = f"Проект {row['Проект']} находится в статусе «{row.get('Статус', 'не указан')}». Ответственный — {row['Ответственный']}."
                            elif "Имя" in row and "Должность" in row:
                                entry = f"{row['Имя']} работает {row['Должность']}. Контакт: email — {row.get('Email', 'не указан')}, телефон — {row.get('Телефон', 'не указан')}."
                            else:
                                entry = "В компании Аквесегмент: " + ", ".join(parts) + "."
                            csv_knowledge.append(entry)
                    if csv_knowledge:
                        logger.info(f"✅ Загружено {len(csv_knowledge)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = csv_knowledge
                    else:
                        logger.warning(f"⚠️ Файл {os.path.basename(file_path)} не содержит данных")
                        
            elif file_path.lower().endswith(".pdf"):
                with open(file_path, 'rb') as f:
                    pdf_reader = PyPDF2.PdfReader(f)
                    logger.info(f"📄 Обработка PDF файла: {os.path.basename(file_path)} (всего страниц: {len(pdf_reader.pages)})")
                    pdf_knowledge = []
                    for page_num, page in enumerate(pdf_reader.pages):
                        text = page.extract_text()
                        if text:
                            paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
                            for paragraph in paragraphs:
                                if len(paragraph) > 50:
                                    pdf_knowledge.append(f"[{os.path.basename(file_path)}, стр. {page_num+1}] {paragraph}")
                    if pdf_knowledge:
                        logger.info(f"✅ Извлечено {len(pdf_knowledge)} фрагментов из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = pdf_knowledge
                    else:
                        logger.warning(f"⚠️ Из файла {os.path.basename(file_path)} не удалось извлечь текст")
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке файла {file_path}: {e}")
    
    def _get_file_embedding_cache_path(self, file_path, knowledge_content):
        """Генерирует путь к кэш-файлу для конкретного файла знаний"""
        content_hash = hashlib.md5(str(sorted(knowledge_content)).encode('utf-8')).hexdigest()
        cache_dir = Path("embeddings_cache")
        cache_dir.mkdir(exist_ok=True)
        filename = Path(file_path).stem
        cache_path = cache_dir / f"{filename}_{content_hash[:12]}.pkl"
        return cache_path

    def _load_or_create_embeddings(self, file_path, knowledge_content):
        """Загружает эмбеддинги из кэша или создает их заново"""
        cache_path = self._get_file_embedding_cache_path(file_path, knowledge_content)
        
        if cache_path.exists():
            logger.info(f"💾 Загрузка эмбеддингов из кэша для {os.path.basename(file_path)}...")
            with open(cache_path, 'rb') as f:
                embeddings = pickle.load(f)
            logger.info(f"✅ Эмбеддинги загружены из кэша для {os.path.basename(file_path)}")
        else:
            logger.info(f"🧠 Создание эмбеддингов для {os.path.basename(file_path)}...")
            embeddings = self.model.encode(knowledge_content, convert_to_tensor=True)
            with open(cache_path, 'wb') as f:
                pickle.dump(embeddings, f)
            logger.info(f"✅ Эмбеддинги сохранены в кэш: {cache_path}")
        
        return embeddings
    
    def reload_knowledge_base(self):
        """Перезагрузка базы знаний"""
        try:
            logger.info("\n🔄 Перезагрузка базы знаний...")
            
            # Загрузка знаний
            self.all_knowledge_dict = self.load_knowledge_from_txt()
            
            # Объединение знаний
            self.my_knowledge = []
            for file_path, knowledge_list in self.all_knowledge_dict.items():
                self.my_knowledge.extend(knowledge_list)
            
            if not self.my_knowledge:
                logger.warning("⚠️ База знаний пуста")
                return False
            
            logger.info(f"📊 Всего загружено {len(self.my_knowledge)} фрагментов знаний из {len(self.all_knowledge_dict)} файлов.")
            
            # Создание эмбеддингов
            logger.info("🧠 Создание/загрузка эмбеддингов...")
            all_embeddings = []
            for file_path, knowledge_list in self.all_knowledge_dict.items():
                file_embeddings = self._load_or_create_embeddings(file_path, knowledge_list)
                all_embeddings.append(file_embeddings)
            
            if all_embeddings:
                if len(all_embeddings) > 1:
                    self.corpus_embeddings = torch.cat(all_embeddings, dim=0)
                else:
                    self.corpus_embeddings = all_embeddings[0]
                
                # Подготовка BM25
                logger.info("🔤 Подготовка лексического поиска (BM25)...")
                self.tokenized_corpus = [doc.split(" ") for doc in self.my_knowledge]
                self.bm25 = BM25Okapi(self.tokenized_corpus)
                
                logger.info(f"✅ База знаний загружена: {len(self.my_knowledge)} фрагментов")
                return True
            else:
                logger.warning("⚠️ Не удалось создать эмбеддинги")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки базы знаний: {e}")
            return False
    
    def find_relevant_info(self, query, top_k=None, alpha=None):
        """Гибридный поиск"""
        # Используем значения из настроек, если не переданы другие
        actual_top_k = top_k if top_k is not None else self.settings.get("search_top_k", 10)
        actual_alpha = alpha if alpha is not None else self.settings.get("search_alpha", 0.7)
        relevance_threshold = self.settings.get("relevance_threshold", 0.2)
        max_context_fragments = self.settings.get("max_context_fragments", 100)
        
        # Проверяем настройку отключения гибридного поиска
        if self.settings.get("disable_hybrid_search", False):
            logger.info("🔤 Гибридный поиск отключен. Передаем всю базу знаний.")
            return "\n".join(self.my_knowledge[:max_context_fragments])
        
        logger.info(f"\n🔍 Поиск релевантной информации для запроса: \"{query}\"")
        
        if not self.my_knowledge or self.corpus_embeddings is None or self.bm25 is None:
            logger.warning("⚠️ База знаний не загружена.")
            return "База знаний не загружена."
        
        # Семантический поиск
        logger.info("🧠 Выполнение семантического поиска...")
        query_embedding = self.model.encode(query, convert_to_tensor=True)
        semantic_hits = util.semantic_search(query_embedding, self.corpus_embeddings, top_k=len(self.my_knowledge))
        semantic_scores = {hit['corpus_id']: hit['score'] for hit in semantic_hits[0]}

        # Лексический поиск (BM25)
        logger.info("🔤 Выполнение лексического поиска (BM25)...")
        tokenized_query = query.split(" ")
        bm25_scores = self.bm25.get_scores(tokenized_query)
        
        # Нормализация BM25 оценок
        if np.max(bm25_scores) > 0:
            bm25_scores_norm = bm25_scores / np.max(bm25_scores)
        else:
            bm25_scores_norm = bm25_scores

        # Комбинирование оценок
        combined_scores = {}
        for i in range(len(self.my_knowledge)):
            sem_score = semantic_scores.get(i, 0.0)
            bm25_score = bm25_scores_norm[i]
            combined_scores[i] = actual_alpha * sem_score + (1 - actual_alpha) * bm25_score

        # Сортировка по комбинированной оценке
        sorted_indices = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)
        
        # Выбор топ-K результатов
        top_indices = sorted_indices[:actual_top_k]
        
        # Фильтрация по порогу
        best_score = combined_scores[top_indices[0]] if top_indices else 0
        if best_score < relevance_threshold:  # <-- Используем настраиваемый порог
            logger.warning(f"⚠️ Низкая релевантность найденных данных (скор: {best_score:.3f}).")
            return "Я не знаю ответа на этот вопрос."

        result = [self.my_knowledge[idx] for idx in top_indices]
        logger.info(f"✅ Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
        return "\n".join(result)
    
    def ask_model(self, question):
        """Отправка запроса ко всем доступным моделям"""
        # Получаем настройки поиска
        search_top_k = self.settings.get("search_top_k", 10)
        search_alpha = self.settings.get("search_alpha", 0.7)
        
        context = self.find_relevant_info(question, top_k=search_top_k, alpha=search_alpha)
        logger.info(f"\n🔍 Переданный контекст моделям:\n{context}")
        
        # Проверяем настройку отключения моделей OpenRouter
        if self.settings.get("disable_openrouter_models", False):
            return f"🔍 Найденный контекст:\n{context}\n\n⚠️ Отправка запросов моделям OpenRouter отключена."
        
        answers = []
        
        for i, model_name in enumerate(AVAILABLE_MODELS):
            try:
                logger.info(f"🤖 Отправка запроса к модели {model_name}...")
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": f"""
Ты — информационный ассистент компании ООО «ГТИ-ОПТ», специализирующейся на оптовой и розничной продаже оборудования для водоочистки и водоподготовки. В твоей базе знаний представлена актуальная информация, которую ты должен использовать для ответов на вопросы клиентов.

Отвечай кратко, ясно и строго на основе информации ниже.

Правила:
- Если ответа нет в информации — скажи «Я не знаю».
- Не выдумывай и не предполагай.
- Анализируй данные и формулируй ответ самостоятельно.
- Отвечай на негативные вопросы и отзывы в соответствии с ответами в базе знаний.
- Отвечай на позитивные вопросы и отзывы своими словами в виде слов благодарности за обращение в компанию.

Информация:
{context}
                        """},
                        {"role": "user", "content": question}
                    ]
                )
                answer = response.choices[0].message.content.strip()
                answers.append(f"Ответ ИИ модели {model_name}:\n{answer}\n")
                logger.info(f"✅ Ответ получен от модели {model_name}")
            except Exception as e:
                error_msg = f"❌ Ошибка при обращении к модели {model_name}: {e}"
                logger.error(error_msg)
                answers.append(f"Ответ ИИ модели {model_name}:\nОшибка: {error_msg}\n")
        
        return "\n---\n".join(answers)  # Разделяем ответы линией для лучшей читаемости

# Создание глобального экземпляра RAG-системы
rag_system = None

def get_rag_system():
    """Получение глобального экземпляра RAG-системы"""
    global rag_system
    if rag_system is None:
        rag_system = RAGCore()
    return rag_system

# Для тестирования и запуска интерфейса настроек
if __name__ == "__main__":
    # Создание и отображение окна настроек
    rag = get_rag_system()
    settings_window = SettingsWindow(settings, rag)
    settings_window.show()



# Запуск веб-интерфейса

def run_web_interface():
    """Запуск веб-интерфейса"""
    try:
        import web_app
        app = web_app.create_app()
        print("🚀 Веб-интерфейс запущен на http://localhost:5000")
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except ImportError:
        print("❌ Не удалось импортировать web_app.py. Убедитесь, что файл существует.")
    except Exception as e:
        print(f"❌ Ошибка запуска веб-интерфейса: {e}")