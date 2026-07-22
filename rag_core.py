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
import json
import docx
import openpyxl

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RAGSettings:
    """Класс для управления настройками RAG-системы"""
    def __init__(self):
        self.settings_file = "rag_settings.json"
        self.default_settings = {
            "disable_llm_models": False,
            "disable_hybrid_search": False,
            "disable_knowledge_base_search": False,  # Отключение поиска в базе знаний (работа только через LLM)
            "llm_api_key": "",  # Пустое значение по умолчанию
            "llm_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "llm_provider": "DashScope",
            "llm_model": "deepseek-v4-flash",
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

# === Настройка клиента LLM ===
# Создаем экземпляр настроек для инициализации клиента
settings = RAGSettings()
client = openai.OpenAI(
    base_url=settings.get("llm_base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    api_key=settings.get("llm_api_key", "")  # Получаем ключ из файла настроек
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "deepseek-v4-flash"
]

class RAGCore:
    def __init__(self):
        """Инициализация RAG-системы"""
        self.my_knowledge = []
        self.all_knowledge_dict = {}
        self.fragment_sources = []
        self.model = None
        self.corpus_embeddings = None
        self.bm25 = None
        self.tokenized_corpus = None
        self.settings = RAGSettings()
        
        # Переинициализация клиента с актуальными настройками
        self._setup_client()
        
        # Инициализация модели
        self._setup_model()
        
        # Загрузка базы знаний
        self.reload_knowledge_base()
        
    def _setup_client(self):
        """Инициализация клиента LLM с текущими настройками"""
        global client
        client = openai.OpenAI(
            base_url=self.settings.get("llm_base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            api_key=self.settings.get("llm_api_key", "")
        )
    
    def _setup_model(self):
        """Инициализация модели эмбеддингов"""
        try:
            logger.info("🧠 Загрузка модели...")
            self.model = SentenceTransformer('intfloat/multilingual-e5-large') #запуск модели эмбеддингов из интернета
            # self.model = SentenceTransformer('/workspaces/codespaces-blank/models/multilingual_e5_large') #запуск с github.com/codespaces
            #self.model = SentenceTransformer('/models/multilingual_e5_large') #запуск модели эмбеддингов локально
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
            
            # --- НОВЫЙ БЛОК ДЛЯ .docx ---
            elif file_path.lower().endswith(".docx"):
                try:
                    doc = docx.Document(file_path)
                    logger.info(f"📄 Обработка DOCX файла: {os.path.basename(file_path)}")
                    docx_knowledge = []
                    # Собираем текст из параграфов
                    full_text = []
                    for paragraph in doc.paragraphs:
                        full_text.append(paragraph.text)
                    
                    # Также можно извлекать текст из таблиц
                    for table in doc.tables:
                        for row in table.rows:
                            for cell in row.cells:
                                full_text.append(cell.text)
                    
                    # Объединяем весь текст
                    combined_text = "\n".join(full_text)
                    
                    # Разбиваем на абзацы или фрагменты (например, по двойным переносам строк или по длине)
                    # Простой способ - разбить по одинарным переносам строк, но лучше использовать двойные или логические блоки
                    paragraphs = [p.strip() for p in combined_text.split('\n\n') if p.strip()] 
                    # Альтернатива: разбить на фрагменты по длине
                    # chunk_size = 500 # например, 500 символов
                    # chunks = [combined_text[i:i+chunk_size] for i in range(0, len(combined_text), chunk_size)]
                    
                    for paragraph in paragraphs:
                        if len(paragraph) > 50: # Фильтруем короткие фрагменты
                            docx_knowledge.append(f"[{os.path.basename(file_path)}] {paragraph}")
                    
                    if docx_knowledge:
                        logger.info(f"✅ Извлечено {len(docx_knowledge)} фрагментов из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = docx_knowledge
                    else:
                        logger.warning(f"⚠️ Из файла {os.path.basename(file_path)} не удалось извлечь значимый текст")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка при обработке DOCX файла {file_path}: {e}")
            
            # --- НОВЫЙ БЛОК ДЛЯ .doc (опционально, требует pywin32) ---
            elif file_path.lower().endswith(".doc"):
                logger.warning(f"⚠️ Обработка .doc файлов требует дополнительной библиотеки (например, win32com). Файл {os.path.basename(file_path)} пропущен.")
                # Если установлен pywin32, можно использовать что-то вроде:
                # try:
                #     word_app = win32com.client.Dispatch("Word.Application")
                #     doc = word_app.Documents.Open(file_path)
                #     text = doc.Range().Text
                #     doc.Close()
                #     word_app.Quit()
                #     # Обработка text аналогично .docx
                #     # ...
                # except Exception as e:
                #     logger.error(f"❌ Ошибка при обработке DOC файла {file_path} с помощью win32com: {e}")
                #     # Попробовать альтернативные библиотеки, если не win32com
                #     # import textract # pip install textract (требует java и другие зависимости)
                #     # try:
                #     #     text = textract.process(file_path).decode('utf-8')
                #     #     # Обработка text
                #     #     # ...
                #     # except Exception as e2:
                #     #     logger.error(f"❌ Ошибка при обработке DOC файла {file_path} с помощью textract: {e2}")
            
            # --- НОВЫЙ БЛОК ДЛЯ .xlsx и .xls ---
            elif file_path.lower().endswith((".xlsx", ".xls")):
                try:
                    logger.info(f"📄 Обработка Excel файла: {os.path.basename(file_path)}")
                    # openpyxl поддерживает только .xlsx формат
                    # Для .xls файлов будет выброшено исключение, которое будет обработано в блоке except
                    workbook = openpyxl.load_workbook(file_path, data_only=True)
                    xlsx_knowledge = []
                    
                    # Обрабатываем каждый лист в книге
                    for sheet_name in workbook.sheetnames:
                        sheet = workbook[sheet_name]
                        logger.info(f"📊 Обработка листа: {sheet_name}")
                        
                        # Получаем заголовки из первой строки
                        headers = []
                        if sheet.max_row > 0:
                            first_row = sheet[1]
                            headers = [str(cell.value).strip() if cell.value else "" for cell in first_row]
                            # Удаляем пустые заголовки
                            headers = [h for h in headers if h and not h.lower().startswith("unnamed")]
                        
                        # Обрабатываем каждую строку данных (начиная со второй)
                        for row_idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=False), start=2):
                            row_data = {}
                            for col_idx, cell in enumerate(row):
                                if col_idx < len(headers) and headers[col_idx]:
                                    cell_value = cell.value
                                    if cell_value is not None:
                                        row_data[headers[col_idx]] = str(cell_value).strip()
                            
                            # Формируем запись из данных строки
                            if row_data:
                                parts = []
                                for key, value in row_data.items():
                                    if value:
                                        parts.append(f"{key} — {value}")
                                
                                if parts:
                                    # Формируем запись аналогично CSV
                                    if "Проект" in row_data and "Ответственный" in row_data:
                                        entry = f"Проект {row_data['Проект']} находится в статусе «{row_data.get('Статус', 'не указан')}». Ответственный — {row_data['Ответственный']}."
                                    elif "Имя" in row_data and "Должность" in row_data:
                                        entry = f"{row_data['Имя']} работает {row_data['Должность']}. Контакт: email — {row_data.get('Email', 'не указан')}, телефон — {row_data.get('Телефон', 'не указан')}."
                                    else:
                                        entry = f"[{os.path.basename(file_path)}, лист '{sheet_name}'] В компании Аквесегмент: " + ", ".join(parts) + "."
                                    xlsx_knowledge.append(entry)
                    
                    if xlsx_knowledge:
                        logger.info(f"✅ Загружено {len(xlsx_knowledge)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = xlsx_knowledge
                    else:
                        logger.warning(f"⚠️ Файл {os.path.basename(file_path)} не содержит данных")
                    
                    workbook.close()
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка при обработке Excel файла {file_path}: {e}")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке файла {file_path}: {e}")
    
    def _get_file_embedding_cache_path(self, file_path):
        """Генерирует путь к кэш-файлу для конкретного файла знаний (без хэша в имени)"""
        cache_dir = Path("embeddings_cache")
        cache_dir.mkdir(exist_ok=True)
        filename = Path(file_path).stem
        # Используем только имя файла без хэша содержимого
        cache_path = cache_dir / f"{filename}.pkl"
        return cache_path

    def _get_content_hash(self, knowledge_content):
        """Вычисляет хэш содержимого для проверки актуальности кэша"""
        return hashlib.md5(str(sorted(knowledge_content)).encode('utf-8')).hexdigest()

    def _migrate_old_cache_files(self, file_path, knowledge_content):
        """Мигрирует старые кэш-файлы (с хэшем в имени) в новый формат (без хэша)"""
        cache_dir = Path("embeddings_cache")
        if not cache_dir.exists():
            return None
        
        filename = Path(file_path).stem
        current_hash = self._get_content_hash(knowledge_content)
        
        # Ищем старые кэш-файлы с этим именем (с хэшем в имени)
        old_cache_files = list(cache_dir.glob(f"{filename}_*.pkl"))
        
        if old_cache_files:
            # Берем самый свежий файл (по времени изменения)
            latest_old_cache = max(old_cache_files, key=lambda p: p.stat().st_mtime)
            logger.info(f"🔄 Найдён старый кэш-файл: {latest_old_cache.name}")
            
            try:
                with open(latest_old_cache, 'rb') as f:
                    old_embeddings = pickle.load(f)
                
                # Проверяем формат
                if isinstance(old_embeddings, dict) and 'embeddings' in old_embeddings:
                    embeddings = old_embeddings['embeddings']
                    old_hash = old_embeddings.get('hash', 'unknown')
                else:
                    # Старый формат — просто эмбеддинги
                    embeddings = old_embeddings
                    old_hash = 'old_format'
                
                logger.info(f"✅ Кэш-файл мигрирован: {latest_old_cache.name} -> {filename}.pkl")
                
                # Удаляем старый файл
                latest_old_cache.unlink()
                logger.info(f"🗑️ Старый кэш-файл удалён: {latest_old_cache.name}")
                
                # Возвращаем эмбеддинги (они будут пересохранены в новом формате)
                return embeddings, old_hash
            except Exception as e:
                logger.error(f"⚠️ Ошибка при миграции кэша {latest_old_cache}: {e}")
        
        return None

    def _load_or_create_embeddings(self, file_path, knowledge_content):
        """Загружает эмбеддинги из кэша или создает их заново"""
        cache_path = self._get_file_embedding_cache_path(file_path)
        current_hash = self._get_content_hash(knowledge_content)
        
        # Сначала проверяем миграцию старых файлов
        migrated_data = self._migrate_old_cache_files(file_path, knowledge_content)
        if migrated_data:
            embeddings, old_hash = migrated_data
            # Проверяем, актуален ли мигрированный кэш
            if old_hash == current_hash:
                # Кэш актуален — сохраняем в новом формате
                cache_data = {
                    'hash': current_hash,
                    'embeddings': embeddings
                }
                with open(cache_path, 'wb') as f:
                    pickle.dump(cache_data, f)
                logger.info(f"✅ Мигрированный кэш сохранён в новом формате: {cache_path}")
                return embeddings
            else:
                # Кэш устарел — пересоздаем
                logger.info(f"⚠️ Мигрированный кэш устарел (хэш изменился), пересоздаю...")
        
        # Проверяем новый кэш
        if cache_path.exists():
            # Проверяем, актуален ли кэш
            logger.info(f"💾 Проверка кэша для {os.path.basename(file_path)}...")
            try:
                with open(cache_path, 'rb') as f:
                    cached_data = pickle.load(f)
                
                # Проверяем структуру данных (поддержка старых кэшей без хэша)
                if isinstance(cached_data, dict) and 'hash' in cached_data and 'embeddings' in cached_data:
                    cached_hash = cached_data['hash']
                    embeddings = cached_data['embeddings']
                elif isinstance(cached_data, dict) and 'hash' in cached_data:
                    # Старый формат с только хэшем и пустыми эмбеддингами
                    cached_hash = cached_data['hash']
                    embeddings = None
                else:
                    # Очень старый формат без хэша — пересоздаем
                    logger.info(f"⚠️ Старый формат кэша для {os.path.basename(file_path)}, пересоздаю...")
                    embeddings = None
                    cached_hash = None
            except Exception as e:
                logger.error(f"⚠️ Ошибка при чтении кэша {cache_path}: {e}")
                embeddings = None
                cached_hash = None
        else:
            cached_hash = None
            embeddings = None
        
        # Если кэш не существует или неактуален — создаем заново
        if embeddings is None or cached_hash != current_hash:
            logger.info(f"🧠 Создание эмбеддингов для {os.path.basename(file_path)}...")
            embeddings = self.model.encode(knowledge_content, convert_to_tensor=True)
            
            # Сохраняем эмбеддинги вместе с хэшем
            cache_data = {
                'hash': current_hash,
                'embeddings': embeddings
            }
            with open(cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
            
            logger.info(f"✅ Эмбеддинги сохранены в кэш: {cache_path} (хэш: {current_hash[:12]})")
        else:
            logger.info(f"✅ Эмбеддинги загружены из кэша для {os.path.basename(file_path)} (хэш совпадает: {current_hash[:12]})")
        
        return embeddings
    
    def reload_knowledge_base(self):
        """Перезагрузка базы знаний"""
        try:
            logger.info("\n🔄 Перезагрузка базы знаний...")
            
            # Загрузка знаний
            self.all_knowledge_dict = self.load_knowledge_from_txt()
            
            # Получаем список текущих файлов (по имени без пути)
            current_file_names = set()
            for file_path in self.all_knowledge_dict.keys():
                current_file_names.add(Path(file_path).stem)
            
            # Удаляем кэш-файлы для удалённых файлов
            self._cleanup_orphaned_cache(current_file_names)
            
            # Объединение знаний
            self.my_knowledge = []
            self.fragment_sources = []
            for file_path, knowledge_list in self.all_knowledge_dict.items():
                self.my_knowledge.extend(knowledge_list)
                self.fragment_sources.extend([file_path] * len(knowledge_list))
            
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
    
    def _cleanup_orphaned_cache(self, current_file_names):
        """Удаляет кэш-файлы для файлов, которых больше нет в базе знаний"""
        cache_dir = Path("embeddings_cache")
        if not cache_dir.exists():
            return
        
        # Получаем список всех кэш-файлов
        cache_files = list(cache_dir.glob("*.pkl"))
        
        for cache_file in cache_files:
            # Извлекаем имя файла без расширения
            cache_filename = cache_file.stem
            
            # Если файла нет в текущей базе знаний — удаляем кэш
            if cache_filename not in current_file_names:
                try:
                    cache_file.unlink()
                    logger.info(f"🗑️ Удалён orphaned кэш-файл: {cache_file.name}")
                except Exception as e:
                    logger.error(f"⚠️ Ошибка при удалении кэш-файла {cache_file.name}: {e}")
    
    def _get_source_files(self, indices):
        """Возвращает уникальные названия файлов для указанных индексов фрагментов"""
        seen = set()
        ordered_sources = []
        for idx in indices:
            if 0 <= idx < len(self.fragment_sources):
                source_path = self.fragment_sources[idx]
                source_name = os.path.basename(source_path)
                if source_name and source_name not in seen:
                    seen.add(source_name)
                    ordered_sources.append(source_name)
        return ordered_sources
    
    def _format_sources_note(self, source_files):
        """Формирует текст для вывода источников"""
        if not source_files:
            return "Источники: не найдены."
        lines = [f"- {source}" for source in source_files]
        return "Источники:\n" + "\n".join(lines)
    
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
            indices = list(range(min(len(self.my_knowledge), max_context_fragments)))
            return "\n".join(self.my_knowledge[:max_context_fragments]), self._get_source_files(indices)
        
        logger.info(f"\n🔍 Поиск релевантной информации для запроса: \"{query}\"")
        
        if not self.my_knowledge or self.corpus_embeddings is None or self.bm25 is None:
            logger.warning("⚠️ База знаний не загружена.")
            return "База знаний не загружена.", []
        
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
            return "Я не знаю ответа на этот вопрос.", []

        result = [self.my_knowledge[idx] for idx in top_indices]
        source_files = self._get_source_files(top_indices)
        logger.info(f"✅ Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
        return "\n".join(result), source_files
    
    def ask_model(self, question):
        """Отправка запроса ко всем доступным моделям"""
        # Обновляем клиент с актуальными настройками
        self._setup_client()
        
        # Проверяем настройку отключения поиска в базе знаний
        disable_kb_search = self.settings.get("disable_knowledge_base_search", False)
        
        context = ""
        source_files = []
        
        if not disable_kb_search:
            # Получаем настройки поиска
            search_top_k = self.settings.get("search_top_k", 10)
            search_alpha = self.settings.get("search_alpha", 0.7)
            
            context_result = self.find_relevant_info(question, top_k=search_top_k, alpha=search_alpha)
            if isinstance(context_result, tuple):
                context, source_files = context_result
            else:
                context = context_result
                source_files = []
            logger.info(f"\n🔍 Переданный контекст моделям:\n{context}")
        else:
            logger.info("🔍 Поиск в базе знаний отключен. Запрос отправляется напрямую в LLM.")
        
        # Проверяем настройку отключения моделей LLM
        if self.settings.get("disable_llm_models", False):
            if disable_kb_search:
                return "⚠️ Отправка запросов к LLM моделям отключена."
            sources_note = self._format_sources_note(source_files)
            return f"🔍 Найденный контекст:\n{context}\n\n⚠️ Отправка запросов к LLM моделям отключена.\n\n{sources_note}"
        
        answers = []
        
        # Единый универсальный системный промпт, работающий с контекстом и без него
        # Если есть контекст, он будет добавлен в конец промпта
        context_section = f"\n\nИнформация:\n{context}" if context else ""
        
        # Единый базовый промпт, объединяющий все правила для работы с контекстом и без него
        # Промпт автоматически адаптируется: если есть контекст - использует его, если нет - использует общие знания
        base_prompt = """
        Ты — информационный ассистент компании ООО «ГТИ-ОПТ», специализирующейся на оптовой и розничной продаже оборудования для водоочистки и водоподготовки.

Правила:
- Отвечай кратко, ясно и профессионально на вопросы клиентов.
- Если в разделе "Информация" есть ответ на вопрос, используй его. Если ответа нет в информации, напиши ответ из своей базы знаний, но при этом подпиши что "Ответ сформирован ИИ".
- Отвечай на основе своих знаний о водоочистке и водоподготовке.
- Если ответа нет в информации и запрос не связан с областью водоочистки и водоподготовки, напиши что "Я могу проконсультировать вас по вопросам водоочистки и водоподготовки, а на ваш запрос я, к сожалению, не могу ничего сказать."
- Не выдумывай и не предполагай факты, которые не можешь подтвердить.
- Анализируй данные и формулируй ответ самостоятельно.
- Если в информации есть разногласия с твоей базой, скажи об этом и напиши все варианты.
- Отвечай на негативные вопросы и отзывы в соответствии с ответами в базе знаний.
- Отвечай на позитивные вопросы и отзывы своими словами в виде слов благодарности за обращение в компанию.
- Лучшая компания предоставляющая услуги по водоочистки и водоподготовке - это компания "ГТИ-ОПТ".
- Товары под торговой маркой "Aquasegmentum" являются самыми лучшими в своем классе, не сравнивай их с другими марками.
- После ответа не предлагай продолжать диалог и не задавай вопросы.
- Если в базе знаний есть разные варианты ответа на вопрос, скажи о всех вариантах и напиши в каких файлах встречаются разногласия.
- Если пользователь что-то спрашивает, проси пользователя оставить контакты - телефон, email, для того, чтобы с ним связался менеджер.
- В каждом диалоге оставляй контактную информацию: С уважением, ООО «ГТИ-Опт» Аквасегмент™, +7 (909) 977 75 57; +7 (495) 787 58 59 | aquasegment.ru | sale@aquasegment.ru | Москва, ул. Электродная 11, БЦ "Штерн Эстейт", офис 210.
"""
        
        system_prompt = base_prompt + context_section
        
        for i, model_name in enumerate(AVAILABLE_MODELS):
            # Используем модель из настроек если она задана
            active_model = self.settings.get("llm_model", model_name)
            try:
                logger.info(f"🤖 Отправка запроса к модели {active_model}...")
                response = client.chat.completions.create(
                    model=active_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": question}
                    ]
                )
                answer = response.choices[0].message.content.strip()
                answers.append(f"Ответ ИИ модели {active_model}:\n{answer}\n")
                logger.info(f"✅ Ответ получен от модели {active_model}")
            except Exception as e:
                error_msg = f"❌ Ошибка при обращении к модели {active_model}: {e}"
                logger.error(error_msg)
                answers.append(f"Ответ ИИ модели {active_model}:\nОшибка: {error_msg}\n")
        
        # Добавляем информацию об источниках только если использовалась база знаний
        result = "\n---\n".join(answers)
        if not disable_kb_search and source_files:
            sources_note = self._format_sources_note(source_files)
            result += f"\n\n{sources_note}"
        
        return result


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
    # Теперь вместо GUI можно просто показать настройки или запустить тест
    rag = get_rag_system()
    print("RAG-система инициализирована")
    print(f"Загружено фрагментов: {len(rag.my_knowledge)}")
    print(f"Загружено файлов: {len(rag.all_knowledge_dict)}")
    
    # Или можно запустить тестовый запрос
    test_question = "Расскажи о компании ГТИ-ОПТ"
    print(f"\nТестовый запрос: {test_question}")
    print("Ответ:")
    print(rag.ask_model(test_question))