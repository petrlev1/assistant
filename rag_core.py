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
import requests
import re
import base64

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Парсер прайс-листов ===

# Ключевые слова колонок прайс-листа (по подстроке, в нижнем регистре)
_PRICE_COL_KEYWORDS = {
    "price": ("цена", "стоимост", "стоим", "розн", "опт", "прайс", "price", "сумма"),
    "name": ("наименовани", "наимен", "товар", "название", "позици", "описани", "продукт", "name", "номенклатур"),
    "article": ("артикул", "код", "article", "sku", "каталожный", "арт."),
    "unit": ("ед.", "ед ", "единиц", "ед.изм", "unit", "изм."),
}


def _classify_header(headers):
    """По строке заголовков определяет индексы колонок price/name/article/unit.
    Возвращает dict индексов или None, если это не похоже на прайс (нет цены + наименования)."""
    idx = {"price": None, "name": None, "article": None, "unit": None}
    for i, h in enumerate(headers):
        h = (h or "").strip().lower()
        if not h:
            continue
        for kind, kws in _PRICE_COL_KEYWORDS.items():
            if idx[kind] is None and any(k in h for k in kws):
                idx[kind] = i
                break
    if idx["price"] is None or idx["name"] is None:
        return None
    return idx


def _parse_price_value(value):
    """'12 500,00 ₽' / 12500.0 → 12500.0; None если это не число"""
    if value is None:
        return None
    s = str(value).strip().replace("\xa0", " ").replace("\u202f", "").replace(" ", "")
    s = s.replace("₽", "").replace("руб.", "").replace("руб", "").replace("р.", "")
    # Евроформат "12.500,00" — точки как разделители тысяч
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "")
    s = s.replace(",", ".")
    # "от 1250" / "1250-1500" → берём первое число
    m = re.search(r"-?\d+(\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _row_to_price_item(row, headers, idx):
    """Строка таблицы → позиция прайса {article, name, price, unit, raw} или None (пустая/мусорная)"""
    def cell(col):
        if col is None or col >= len(row):
            return ""
        v = row[col]
        return str(v).strip() if v is not None else ""

    name = cell(idx["name"])
    if not name:
        return None
    low = name.lower()
    if any(w in low for w in ("итого", "всего:", "сумма по")):
        return None
    price = _parse_price_value(cell(idx["price"]) if idx["price"] is not None else None)
    if price is None:
        return None  # строка без цены для прайса бесполезна
    article = cell(idx["article"]) if idx["article"] is not None else ""
    unit = cell(idx["unit"]) if idx["unit"] is not None else ""
    raw = {headers[i]: v for i, v in enumerate(row) if i < len(headers) and headers[i] and v is not None and str(v).strip()}
    return {"article": article, "name": name, "price": price, "unit": unit, "raw": raw}


def _extract_price_rows(rows, max_header_scan=15):
    """Ищет строку-шапку среди первых N строк и извлекает позиции.
    Возвращает список dict-позиций или [] — если это не прайс (нет шапки или ни одной строки с ценой)."""
    for i in range(min(len(rows), max_header_scan)):
        headers = [str(c).strip() if c is not None else "" for c in rows[i]]
        idx = _classify_header(headers)
        if not idx:
            continue
        parsed = []
        for row in rows[i + 1:]:
            item = _row_to_price_item(row, headers, idx)
            if item:
                parsed.append(item)
        if parsed:
            logger.info(f"📋 Шапка прайса найдена в строке {i + 1}: {[h for h in headers if h][:8]}")
            return parsed
    return []


def _iter_csv_rows(file_path):
    """Читает CSV (utf-8/cp1251 — русские прайсы бывают в cp1251) в список списков"""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            with open(file_path, "r", encoding=enc, newline="") as f:
                return list(csv.reader(f))
        except (UnicodeDecodeError, UnicodeError):
            continue
    return []


def _iter_xlsx_rows(file_path):
    """Читает листы .xlsx (openpyxl не умеет .xls) и возвращает позиции первого листа-прайса"""
    workbook = openpyxl.load_workbook(file_path, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            rows = [list(r) for r in sheet.iter_rows(values_only=True)]
            parsed = _extract_price_rows(rows)
            if parsed:
                logger.info(f"📊 Прайс найден на листе «{sheet_name}»: {len(parsed)} позиций")
                return parsed
    finally:
        workbook.close()
    return []


def parse_price_list(file_path):
    """Определяет, является ли файл прайс-листом, и извлекает позиции.
    Поддерживает .xlsx и .csv. Возвращает список dict {article, name, price, unit, raw};
    пустой список — файл не похож на прайс или не поддерживается."""
    ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else ""
    try:
        if ext == "xlsx":
            return _iter_xlsx_rows(file_path)
        if ext == "csv":
            return _extract_price_rows(_iter_csv_rows(file_path))
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга прайс-листа {file_path}: {e}")
    return []


# Детект ценового вопроса: «цена», «сколько стоит», «артикул», «₽» и т.п.
_PRICE_INTENT_RE = re.compile(
    r"(цена|цены|цену|стоимость|стоит|стоят|сколько стоит|прайс|артикул|арт\.?"
    r"|руб\b|₽|рублей|дешевле|дороже|дешёвле|дёшево|дорого|скидк|price)",
    re.IGNORECASE,
)


def detect_price_intent(question):
    """True, если вопрос похож на ценовой (тогда сначала ищем в прайс-листе)"""
    return bool(_PRICE_INTENT_RE.search(question or ""))


class RAGSettings:
    """Класс для управления настройками RAG-системы"""
    def __init__(self):
        self.settings_file = "rag_settings.json"
        self.default_settings = {
            "disable_llm_models": False,
            "disable_hybrid_search": False,
            "disable_knowledge_base_search": False,  # Отключение поиска в базе знаний (работа только через LLM)
            "llm_api_key": "",  # Пустое значение по умолчанию (используется и для OCR DashScope)
            "llm_provider_api_key": "",  # Отдельный ключ для LLM-провайдера (например DeepSeek); fallback — llm_api_key
            "llm_openrouter_api_key": "",  # Ключ OpenRouter (sk-or-...); fallback — llm_provider_api_key / llm_api_key
            "llm_base_url": "https://api.deepseek.com/v1",
            "llm_provider": "DeepSeek",
            "llm_model": "deepseek-v4-flash",
            # Настройки поиска
            "search_top_k": 10,
            "search_alpha": 0.7,
            "relevance_threshold": 0.2,
            "max_context_fragments": 100,
            # Настройки OCR для сканированных PDF (DashScope qwen-vl-ocr)
            "ocr_enabled": False,
            "ocr_model": "qwen-vl-ocr",
            "ocr_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "ocr_dpi": 150
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
        """Сохранение настроек в файл (атомарно: tmp + rename — веб-сервер может читать файл параллельно)"""
        try:
            tmp_file = self.settings_file + '.tmp'
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
            os.replace(tmp_file, self.settings_file)
            logger.info("Настройки сохранены")
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")
    
    def get(self, key, default=None):
        """Получение значения настройки"""
        return self.settings.get(key, default)

    def reload(self):
        """Перечитать настройки из файла (изменения из лаунчера применяются без рестарта веб-сервера).
        При ошибке чтения/парсинга — оставить текущие настройки, НЕ сбрасывать на defaults."""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    for key, value in self.default_settings.items():
                        if key not in settings:
                            settings[key] = value
                    self.settings = settings
                    logger.info("⚙️ Настройки перечитаны из файла")
        except Exception as e:
            logger.error(f"Ошибка перечитывания настроек: {e} — оставлены текущие")
    
    def get_llm_api_key(self):
        """Ключ API для LLM-провайдера: DeepSeek → llm_provider_api_key (fallback llm_api_key),
        OpenRouter → llm_openrouter_api_key (fallback llm_provider_api_key / llm_api_key),
        остальные (DashScope и т.п.) → llm_api_key (он же используется для OCR)."""
        provider = self.settings.get("llm_provider", "DeepSeek")
        if provider == "DeepSeek":
            return self.settings.get("llm_provider_api_key") or self.settings.get("llm_api_key", "")
        if provider == "OpenRouter":
            return (self.settings.get("llm_openrouter_api_key")
                    or self.settings.get("llm_provider_api_key")
                    or self.settings.get("llm_api_key", ""))
        return self.settings.get("llm_api_key", "")
    
    def set(self, key, value):
        """Установка значения настройки"""
        self.settings[key] = value

# === Настройка клиента LLM ===
# Создаем экземпляр настроек для инициализации клиента
settings = RAGSettings()
client = openai.OpenAI(
    base_url=settings.get("llm_base_url", "https://api.deepseek.com/v1"),
    api_key=settings.get_llm_api_key()  # Ключ зависит от выбранного провайдера
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "deepseek-v4-flash"
]

# Стандартный системный промт (используется, если у пользователя нет персонального)
DEFAULT_BASE_PROMPT = """Ты - информационный ассистент.
Правила:
- Отвечай кратко, ясно и профессионально на вопросы клиентов.
- Отвечай на основе своих знаний.
- Не выдумывай и не предполагай факты, которые не можешь подтвердить.
- Анализируй данные и формулируй ответ самостоятельно.
- Если в информации есть разногласия с твоей базой, скажи об этом и напиши все варианты.
- Если в базе знаний есть разные варианты ответа на вопрос, скажи о всех вариантах и напиши в каких файлах встречаются разногласия.
"""


def extract_bot_identity(prompt):
    """Извлекает из системного промта, кем является бот (первая строка вида «Ты — ...»).
    Возвращает строку роли или None, если определить не удалось."""
    if not prompt:
        return None
    first_line = prompt.strip().split("\n", 1)[0].strip()
    # Ищем «Ты —», «Ты -», «Ты:» в начале первой строки
    for prefix in ("ты —", "ты -", "ты:", "ты—", "ты-"):
        if first_line.lower().startswith(prefix):
            role = first_line[len(prefix):].strip()
            # Обрезаем до конца первого предложения или запятой с пояснением
            for sep in (".", "!", "?"):
                idx = role.find(sep)
                if idx != -1:
                    role = role[:idx].strip()
                    break
            return role or None
    return None


def build_greeting(prompt, username="Пользователь"):
    """Формирует приветственное сообщение: бот представляется ролью из промта.
    Если роль не определена — классическое приветствие с именем пользователя."""
    role = extract_bot_identity(prompt)
    if role:
        return f"Привет! Я — {role}. Задай мне вопрос по базе знаний."
    return f"Привет, {username}! Задай мне вопрос по базе знаний."

class RAGCore:
    def __init__(self, user_id=None):
        """Инициализация RAG-системы"""
        self.my_knowledge = []
        self.all_knowledge_dict = {}
        self.fragment_sources = []
        self.model = None
        self.corpus_embeddings = None
        self.bm25 = None
        self.tokenized_corpus = None
        self.settings = RAGSettings()
        self.current_user_id = user_id  # None = общая база знаний
        
        # Переинициализация клиента с актуальными настройками
        self._setup_client()
        
        # Инициализация модели
        self._setup_model()
        
        # Загрузка базы знаний
        self.reload_knowledge_base(user_id)
        
    def _sanitize_text(self, text):
        """Очистка текста от символов, которые могут вызвать проблемы с кодировкой на Windows"""
        if not text:
            return text
        # Заменяем только проблемные символы, не входящие в latin-1
        # (httpx/openai на Windows может падать на них)
        replacements = {
            '\u2026': '...',  # горизонтальное многоточие …
            '\u2013': '-',    # короткое тире –
            '\u2014': '--',   # длинное тире —
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def _setup_client(self):
        """Инициализация клиента LLM с текущими настройками"""
        global client
        client = openai.OpenAI(
            base_url=self.settings.get("llm_base_url", "https://api.deepseek.com/v1"),
            api_key=self.settings.get_llm_api_key()
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
    
    def load_for_user(self, user_id):
        """Переключение на базу знаний конкретного пользователя"""
        self.current_user_id = user_id
        self.reload_knowledge_base(user_id)
        logger.info(f"👤 RAG переключён на пользователя #{user_id}")
    
    def _apply_txt_priority(self, paths):
        """Приоритет TXT: если для файла есть одноимённый .txt — в эмбеддинги берём только .txt.

        Пример: в базе лежат myfile.pdf и myfile.txt. Эмбеддинги строятся по myfile.txt,
        а myfile.pdf остаётся на диске и доступен только для скачивания
        (Документы, источники в чате). Работает для любых поддерживаемых форматов.
        """
        txt_stems = {Path(p).stem.lower() for p in paths if str(p).lower().endswith(".txt")}
        kept = []
        for p in paths:
            if not str(p).lower().endswith(".txt") and Path(p).stem.lower() in txt_stems:
                logger.info(f"⏭️ {os.path.basename(p)} пропущен для эмбеддингов — есть одноимённый .txt (приоритет TXT)")
                continue
            kept.append(p)
        return kept

    def load_knowledge_from_txt(self, file_paths=None, user_id=None):
        """Загрузка знаний из файлов"""
        all_knowledge = {}
        
        # Если файлы не указаны, загружаем из стандартных мест
        if not file_paths:
            logger.info("📂 Загрузка базы знаний из стандартных источников...")
            
            if user_id is not None:
                # Загрузка базы знаний конкретного пользователя
                user_db_folder = os.path.join("Database", f"user_{user_id}")
                if os.path.exists(user_db_folder):
                    logger.info(f"📁 Загрузка базы знаний пользователя #{user_id} из {user_db_folder}")
                    folder_paths = [
                        os.path.join(user_db_folder, f)
                        for f in os.listdir(user_db_folder)
                        if os.path.isfile(os.path.join(user_db_folder, f))
                    ]
                    for file_path in self._apply_txt_priority(folder_paths):
                        self._process_file(file_path, all_knowledge)
                else:
                    logger.info(f"📁 Папка пользователя #{user_id} не найдена, создаю пустую базу")
                    os.makedirs(user_db_folder, exist_ok=True)
            else:
                # Загрузка общей базы знаний (стандартное поведение)
                self._load_common_knowledge(all_knowledge)
        else:
            # Загрузка указанных файлов
            logger.info(f"📂 Загрузка {len(file_paths)} выбранных файлов...")
            for file_path in self._apply_txt_priority(file_paths):
                logger.info(f"📄 Обработка файла: {file_path}")
                self._process_file(file_path, all_knowledge)
        
        return all_knowledge
    
    def _load_common_knowledge(self, all_knowledge):
        """Загрузка общей базы знаний (DataBase.txt + Database/)"""
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
            folder_paths = [
                os.path.join(database_folder, f)
                for f in os.listdir(database_folder)
                if os.path.isfile(os.path.join(database_folder, f)) and not f.startswith("user_")
            ]
            for file_path in self._apply_txt_priority(folder_paths):
                self._process_file(file_path, all_knowledge)
        else:
            logger.warning(f"⚠️ Папка {database_folder} не найдена")
    
    def _is_garbage_text(self, text, min_cyrillic_ratio=0.1, max_garbage_ratio=0.2):
        """Проверка, что фрагмент текста — не «каракули» (сломанная кодировка PDF).
        Возвращает True, если текст мусорный и его НЕ нужно добавлять в базу знаний."""
        if not text:
            return True
        text = text.strip()
        if len(text) < 10:
            return True

        # 1) Признак битой кодировки: токены вида /uniXXXX (PyPDF2 часто выдаёт это)
        uni_tokens = re.findall(r'/uni[0-9A-Fa-f]{4}', text)
        if len(uni_tokens) >= 3:
            return True

        # 2) Доля «мусорных» символов: всё, что не буквы/цифры/пробел/базовая пунктуация
        letters = sum(1 for ch in text if ch.isalpha())
        digits = sum(1 for ch in text if ch.isdigit())
        spaces = sum(1 for ch in text if ch.isspace())
        basic_punct = sum(1 for ch in text if ch in '.,;:!?()[]«»"\'—-–%№/+*=<>')
        meaningful = letters + digits + spaces + basic_punct
        garbage_ratio = 1.0 - meaningful / max(len(text), 1)
        if garbage_ratio > max_garbage_ratio:
            return True

        # 3) Кириллица: если в тексте вообще нет русских букв — подозрительно
        #    (в базе знаний компании документы в основном на русском).
        #    Но не отбрасываем чисто технические/латинские фрагменты полностью,
        #    если они выглядят осмысленно (много букв).
        cyrillic = sum(1 for ch in text if 'А' <= ch <= 'я' or ch in 'Ёё')
        if letters > 0 and cyrillic / letters < min_cyrillic_ratio:
            # Если текст короткий и без кириллицы — скорее всего артефакт.
            # Если длинный и читаемый на латинице (например, инструкция на английском) — пропускаем.
            if len(text) < 200:
                return True

        # 4) Много подряд идущих несмысловых символов (например, ######## или /////)
        if re.search(r'([^\w\sА-Яа-яЁё]{5,})', text):
            return True

        return False

    def _get_ocr_cache_path(self, file_path, page_num):
        """Путь к кэшу OCR-текста страницы (по хэшу PDF-файла), чтобы не жечь деньги
        на повторных переиндексациях."""
        if self.current_user_id is not None:
            cache_dir = Path("embeddings_cache") / f"user_{self.current_user_id}" / "ocr_cache"
        else:
            cache_dir = Path("embeddings_cache") / "ocr_cache"
        file_hash = hashlib.md5(open(file_path, 'rb').read()).hexdigest()[:12]
        page_dir = cache_dir / file_hash
        page_dir.mkdir(exist_ok=True, parents=True)
        return page_dir / f"page_{page_num+1}.txt"

    def _ocr_page(self, page, base_name, page_num, cache_path):
        """OCR страницы через DashScope qwen-vl-ocr. Возвращает распознанный текст
        или None, если OCR отключён / не удался."""
        api_key = self.settings.get("llm_api_key", "")
        if not api_key:
            logger.info(f"⚠️ OCR отключён (llm_api_key не задан) — страница {page_num+1} файла {base_name} пропущена")
            return None

        # Кэш: не вызываем API повторно для той же страницы
        if cache_path.exists():
            logger.info(f"💾 OCR из кэша: {cache_path.name} ({base_name})")
            return cache_path.read_text(encoding='utf-8')

        ocr_model = self.settings.get("ocr_model", "qwen-vl-ocr")
        ocr_base_url = self.settings.get("ocr_base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
        ocr_dpi = int(self.settings.get("ocr_dpi", 150))

        try:
            # Рендерим страницу в PNG
            pix = page.get_pixmap(dpi=ocr_dpi)
            img_b64 = base64.b64encode(pix.tobytes("png")).decode()
            logger.info(f"🔍 OCR страницы {page_num+1} файла {base_name} через {ocr_model}...")

            payload = {
                "model": ocr_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                    ]
                }],
            }
            resp = requests.post(
                f"{ocr_base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=180,
            )
            if resp.status_code != 200:
                logger.error(f"❌ OCR ошибка ({resp.status_code}): {resp.text[:200]}")
                return None
            text = resp.json()["choices"][0]["message"]["content"]
            if text:
                cache_path.write_text(text, encoding='utf-8')
            return text
        except Exception as e:
            logger.error(f"❌ Ошибка OCR страницы {page_num+1} ({base_name}): {e}")
            return None

    def _extract_pdf_text(self, file_path):
        """Извлечение текста из PDF: PyMuPDF (качественный) с фолбэком на PyPDF2.
        Пустые/мусорные страницы (сканы) распознаются через OCR (fal.ai), если он включён.
        Возвращает список фрагментов с префиксом источника."""
        base_name = os.path.basename(file_path)
        pdf_knowledge = []
        ocr_enabled = bool(self.settings.get("ocr_enabled", False))

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(file_path)
            try:
                logger.info(f"📄 Обработка PDF (PyMuPDF): {base_name} (всего страниц: {len(doc)})")
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    page_fragments = []
                    # blocks + sort=True: правильный порядок чтения и отсев дублей
                    # текстового слоя (дизайнерские PDF и буклеты часто дублируют текст)
                    blocks = page.get_text("blocks", sort=True)
                    for block in blocks:
                        paragraph = block[4].strip()
                        if not paragraph:
                            continue
                        # Склеиваем переносы строк внутри блока в один пробел
                        paragraph = " ".join(line.strip() for line in paragraph.split('\n') if line.strip())
                        if len(paragraph) > 50 and not self._is_garbage_text(paragraph):
                            page_fragments.append(f"[{base_name}, стр. {page_num+1}] {paragraph}")

                    # Если текстовый слой не дал результата (скан или битая кодировка) — пробуем OCR
                    if not page_fragments and ocr_enabled:
                        cache_path = self._get_ocr_cache_path(file_path, page_num)
                        ocr_text = self._ocr_page(page, base_name, page_num, cache_path)
                        if ocr_text:
                            paragraphs = [p.strip() for p in ocr_text.split('\n') if p.strip()]
                            for paragraph in paragraphs:
                                if len(paragraph) > 50 and not self._is_garbage_text(paragraph):
                                    page_fragments.append(f"[{base_name}, стр. {page_num+1}] {paragraph}")

                    pdf_knowledge.extend(page_fragments)
            finally:
                doc.close()
        except ImportError:
            # Фолбэк: старый PyPDF2 (если PyMuPDF не установлен)
            logger.info(f"📄 Обработка PDF (PyPDF2 fallback): {base_name}")
            with open(file_path, 'rb') as f:
                pdf_reader = PyPDF2.PdfReader(f)
                for page_num, page in enumerate(pdf_reader.pages):
                    text = page.extract_text()
                    if text:
                        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
                        for paragraph in paragraphs:
                            if len(paragraph) > 50 and not self._is_garbage_text(paragraph):
                                pdf_knowledge.append(f"[{base_name}, стр. {page_num+1}] {paragraph}")
        return pdf_knowledge

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
                pdf_knowledge = self._extract_pdf_text(file_path)
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
        """Генерирует путь к кэш-файлу для конкретного файла знаний (с учётом пользователя)"""
        if self.current_user_id is not None:
            cache_dir = Path("embeddings_cache") / f"user_{self.current_user_id}"
        else:
            cache_dir = Path("embeddings_cache")
        cache_dir.mkdir(exist_ok=True, parents=True)
        filename = Path(file_path).stem
        cache_path = cache_dir / f"{filename}.pkl"
        return cache_path

    def _get_content_hash(self, knowledge_content):
        """Вычисляет хэш содержимого для проверки актуальности кэша"""
        return hashlib.md5(str(sorted(knowledge_content)).encode('utf-8')).hexdigest()

    def _migrate_old_cache_files(self, file_path, knowledge_content):
        """Мигрирует старые кэш-файлы (с хэшем в имени) в новый формат (без хэша)"""
        if self.current_user_id is not None:
            cache_dir = Path("embeddings_cache") / f"user_{self.current_user_id}"
        else:
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
    
    def reload_knowledge_base(self, user_id=None):
        """Перезагрузка базы знаний"""
        try:
            logger.info("\n🔄 Перезагрузка базы знаний...")
            
            # Загрузка знаний
            self.all_knowledge_dict = self.load_knowledge_from_txt(user_id=user_id)
            
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
                # Сбрасываем эмбеддинги и BM25
                self.corpus_embeddings = None
                self.bm25 = None
                self.tokenized_corpus = None
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
    
    def _get_cache_dir(self):
        """Возвращает директорию кэша с учётом текущего пользователя"""
        if self.current_user_id is not None:
            return Path("embeddings_cache") / f"user_{self.current_user_id}"
        return Path("embeddings_cache")
    
    def _cleanup_orphaned_cache(self, current_file_names):
        """Удаляет кэш-файлы для файлов, которых больше нет в базе знаний"""
        cache_dir = self._get_cache_dir()
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
    
    def _get_display_source_name(self, source_path):
        """Отображаемое имя источника.

        Если фрагмент получен из .txt, рядом с которым лежит одноимённый файл
        другого формата (оригинал, например PDF) — показываем оригинал,
        чтобы в списке «Источники:» было настоящее имя документа.
        """
        name = os.path.basename(source_path)
        if not name.lower().endswith('.txt'):
            return name
        folder = os.path.dirname(source_path)
        if not folder or not os.path.isdir(folder):
            return name
        stem = os.path.splitext(name)[0].lower()
        siblings = [
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and os.path.splitext(f)[0].lower() == stem
            and f.lower() != name.lower()
        ]
        if siblings:
            siblings.sort(key=lambda f: (not f.lower().endswith('.pdf'), f.lower()))
            return siblings[0]
        return name

    def _get_source_files(self, indices):
        """Возвращает уникальные названия файлов для указанных индексов фрагментов"""
        seen = set()
        ordered_sources = []
        for idx in indices:
            if 0 <= idx < len(self.fragment_sources):
                source_path = self.fragment_sources[idx]
                source_name = self._get_display_source_name(source_path)
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
    
    def _try_price_answer(self, question):
        """Точный путь: ценовой вопрос → поиск по прайс-листу (PostgreSQL).
        Возвращает готовый ответ (факты из таблицы, без LLM — никаких галлюцинаций цены)
        или None — тогда выполняется обычный RAG-путь."""
        if not detect_price_intent(question):
            return None
        user_id = self.current_user_id
        if user_id is None:
            return None
        try:
            from auth_db import search_price_items
            rows = search_price_items(user_id, question)
        except Exception as e:
            logger.error(f"Ошибка поиска по прайс-листу: {e}")
            return None
        if not rows:
            logger.info("💲 Ценовой интент, но по прайсу ничего не найдено — обычный RAG-путь")
            return None
        logger.info(f"💲 По прайс-листу найдено {len(rows)} позиций")
        lines = []
        for r in rows:
            parts = []
            if r.get("article"):
                parts.append(f"арт. {r['article']}")
            unit = f" за {r['unit']}" if r.get("unit") else ""
            price = self._format_price(r.get("price"))
            name = r.get("name", "")
            suffix = f" ({', '.join(parts)})" if parts else ""
            lines.append(f"{name}{suffix} — {price}{unit}")
        answer = "💲 По прайс-листу:\n" + "\n".join(lines)
        sources = sorted({r["filename"] for r in rows})
        answer += "\n\n" + self._format_sources_note(sources)
        return answer

    @staticmethod
    def _format_price(price):
        """12500.0 → '12 500 ₽'; None → 'цена не указана'"""
        if price is None:
            return "цена не указана"
        try:
            val = float(price)
        except (TypeError, ValueError):
            return "цена не указана"
        if val == int(val):
            s = f"{int(val):,}".replace(",", " ")
        else:
            s = f"{val:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} ₽"

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
    
    def ask_model(self, question, user_prompt=None):
        """Отправка запроса ко всем доступным моделям
        user_prompt: персональный системный промт пользователя (если задан — используется вместо системного по умолчанию)
        """
        # Перечитываем настройки из файла — изменения из лаунчера применяются без рестарта сервера
        self.settings.reload()
        # Обновляем клиент с актуальными настройками
        self._setup_client()

        # Точный путь: ценовой вопрос → поиск по прайс-листу (PostgreSQL).
        # Если найдено — отвечаем фактами из таблицы без LLM.
        price_answer = self._try_price_answer(question)
        if price_answer:
            if self.settings.get("disable_llm_models", False):
                price_answer += "\n\n⚠️ Отправка запросов к LLM моделям отключена."
            return price_answer
        
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
                output = "🔍 Поиск в базе знаний отключен.\nКонтекст не загружен."
            elif context == "База знаний не загружена." or not context:
                output = f"🔍 Контекст: {context}"
            else:
                output = f"🔍 Найденный контекст:\n{context}"
            sources_note = self._format_sources_note(source_files) if not disable_kb_search else "Источники: не найдены (поиск в БЗ отключен)."
            return f"{output}\n\n⚠️ Отправка запросов к LLM моделям отключена.\n\n{sources_note}"
        answers = []
        
        # Единый универсальный системный промпт, работающий с контекстом и без него
        # Если есть контекст, он будет добавлен в конец промпта
        context_section = f"\n\nИнформация:\n{context}" if context else ""
        
        # Единый базовый промпт, объединяющий все правила для работы с контекстом и без него
        # Промпт автоматически адаптируется: если есть контекст - использует его, если нет - использует общие знания
        # Если пользователь задал свой персональный промт — используем его вместо системного по умолчанию
        base_prompt = (user_prompt or "").strip() or DEFAULT_BASE_PROMPT
        if user_prompt and user_prompt.strip():
            logger.info("📝 Используется персональный промт пользователя")
        else:
            logger.info("📝 Используется системный промт по умолчанию")
        
        system_prompt = base_prompt + context_section
        # Очищаем текст от символов, не поддерживаемых ascii (проблема Windows)
        if '\u2026' in system_prompt:
            logger.info("⚠️ Найден символ \\u2026 в system_prompt, заменяю...")
        system_prompt = self._sanitize_text(system_prompt)
        if '\u2026' in question:
            logger.info("⚠️ Найден символ \\u2026 в question, заменяю...")
        question = self._sanitize_text(question)
        
        for i, model_name in enumerate(AVAILABLE_MODELS):
            # Используем модель из настроек если она задана
            active_model = self.settings.get("llm_model", model_name)
            try:
                logger.info(f"🤖 Отправка запроса к модели {active_model}...")
                # Используем requests напрямую (httpx на Windows может падать на Unicode)
                response = requests.post(
                f"{self.settings.get('llm_base_url', 'https://api.deepseek.com/v1')}/chat/completions",
                    headers={
                "Authorization": f"Bearer {self.settings.get_llm_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                "model": active_model,
                    "messages": [
                {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question}
                ]
                    },
                timeout=60
                )
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]["content"].strip()
                answers.append(f"{answer}")
                logger.info(f"✅ Ответ получен от модели {active_model}")
            except Exception as e:
                error_msg = f"❌ Ошибка при обращении к модели {active_model}: {e}"
                # На Windows str(e) может содержать символы, ломающие ascii-кодировку
                error_msg_safe = error_msg.encode('utf-8', errors='replace').decode('utf-8')
                logger.error(error_msg_safe)
                answers.append(f"Ответ ИИ модели {active_model}:\nОшибка: {error_msg_safe}\n")
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