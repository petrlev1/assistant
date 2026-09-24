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
import io
import threading

import model_catalog

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Исправления ответов пользователем (Вариант B) ===
# Кнопка «✏️» у ответа в чате: неверный ответ правится, пара Вопрос-Ответ
# сохраняется в Database/user_{id}/newdatabase.csv (CSV: «Вопрос,Ответ»). При повторном похожем
# вопросе отвечаем исправлением напрямую (без LLM).
QA_CORRECTION_FILE = "newdatabase.csv"
QA_CORRECTION_THRESHOLD = 0.92  # мин. косинусная схожесть вопроса с исправлением (e5 даёт высокий базовый фон)
QA_CORRECTION_MARGIN = 0.025  # мин. отрыв лучшего исправления от второго (при нескольких исправлениях)

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
            # БЕЗ break: одна ячейка может нести несколько ролей сразу
            # (например, "IE_NAME;CR_PRICE_1_RUB" — имя и цена в одной ячейке)
            if idx[kind] is None and any(k in h for k in kws):
                idx[kind] = i
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
    """Читает CSV (utf-8/cp1251) в список списков; разделитель определяется автоматически
    (';' — типично для выгрузок 1С/CRM, ',' — Excel, '\t' — копипаста из таблиц)."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            with open(file_path, "r", encoding=enc, newline="") as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    delim = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
                except csv.Error:
                    delim = ","
                return list(csv.reader(f, delimiter=delim))
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


# === Детекция группы документа (по имени файла + превью содержимого) ===
# Порядок правил важен: первое совпадение выигрывает.
_DOC_GROUP_PATTERNS = [
    (r'инструкц|instruction|manual|guide', '📖 Инструкции'),
    (r'паспорт|passport', '📋 Паспорт товара'),
    (r'руководств|эксплуатац|operation|operating', '📘 Руководства'),
    (r'каталог|catalog|catalogue', '📚 Каталоги'),
    (r'сертификат|certificat', '📜 Сертификаты'),
    (r'договор|contract|agreement', '🤝 Договоры'),
    (r'\bсчет|\bсчёт|invoice|\bbill', '🧾 Счета'),
    (r'накладн|waybill|delivery note', '📦 Накладные'),
    (r'характеристик|спецификац|specification|\bspec\b|datasheet', '📊 Характеристики'),
    (r'описани|description', '📝 Описания'),
    (r'брошюр|буклет|brochure', '📗 Брошюры'),
    (r'презентац|presentation', '🖥️ Презентации'),
    (r'таблиц|расчет|расчёт|calculation|\btable', '🗂️ Таблицы'),
    (r'\bформ|бланк|заявк|\bform\b|\bapplication\b', '📄 Формы и бланки'),
]


def _read_text_preview(file_path, max_chars=3000):
    """Быстрое извлечение первых max_chars символов текста файла для детекции группы.
    Поддерживает .txt/.csv/.pdf/.docx/.xlsx; никогда не бросает исключений."""
    ext = file_path.lower().rsplit('.', 1)[-1] if '.' in file_path else ''
    text = ''
    try:
        if ext in ('txt', 'csv'):
            with open(file_path, 'rb') as f:
                raw = f.read(max_chars * 2)
            for enc in ('utf-8', 'utf-8-sig', 'cp1251'):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        elif ext == 'pdf':
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(file_path)
                try:
                    for i in range(min(3, len(doc))):
                        text += doc[i].get_text() or ''
                        if len(text) >= max_chars:
                            break
                finally:
                    doc.close()
            except ImportError:
                reader = PyPDF2.PdfReader(file_path)
                for i in range(min(3, len(reader.pages))):
                    text += reader.pages[i].extract_text() or ''
                    if len(text) >= max_chars:
                        break
        elif ext == 'docx':
            doc = docx.Document(file_path)
            for p in doc.paragraphs[:60]:
                text += (p.text or '') + '\n'
        elif ext == 'xlsx':
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            try:
                ws = wb.worksheets[0]
                for row in ws.iter_rows(max_row=10, values_only=True):
                    text += ' '.join(str(c) for c in row if c is not None) + '\n'
            finally:
                wb.close()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось извлечь текст для детекции группы {file_path}: {e}")
    return text[:max_chars]


def detect_doc_group(filename='', preview_text='', is_price_list=False):
    """Определяет группу документа по имени файла и превью содержимого.
    Сначала флаг прайс-листа, затем ключевые слова (RU+EN) в склеенном тексте.
    Возвращает название группы с эмодзи (напр. '📖 Инструкции') или '📁 Прочие документы'."""
    if is_price_list:
        return '💲 Прайс-листы'
    haystack = f"{filename or ''} {preview_text or ''}".lower()
    for pattern, group in _DOC_GROUP_PATTERNS:
        if re.search(pattern, haystack):
            return group
    return '📁 Прочие документы'


def detect_price_intent(question):

    """True, если вопрос похож на ценовой (тогда сначала ищем в прайс-листе)"""
    return bool(_PRICE_INTENT_RE.search(question or ""))


# === Память диалога (вариант B) ===
# История уходит в messages (модель видит предыдущие ходы), а поиск по базе знаний —
# по «осмысленному» запросу: прошлые вопросы пользователя + текущий. Это лечит follow-up
# («а второй сколько стоит?»), по которому гибридный поиск сам по себе не находит ничего.
_SOURCES_BLOCK_RE = re.compile(r'\n*Источники:.*$', re.S)


def _norm_question(text):
    """Нормализация вопроса для сравнения (регистр и пробелы)."""
    return ' '.join((text or '').lower().split())


def _prepare_chat_history(history, question):
    """История диалога для промпта: [{'role','message'}] в хронологическом порядке.

    * Текущий вопрос уже сохранён в chat_history до вызова модели (так работает и веб-чат,
      и виджет) — последнюю запись с тем же текстом убираем, иначе вопрос дублируется.
    * Из ответов бота вырезаем блок «Источники: …» — это внутренняя служебная информация,
      в промпте она только мешает (и модель начинала бы перечислять файлы базы).
    """
    if not history:
        return []
    items = [{'role': str(h.get('role') or ''), 'message': h.get('message') or ''}
             for h in history if h and (h.get('message') or '').strip()]
    if items and items[-1]['role'] == 'user' and _norm_question(items[-1]['message']) == _norm_question(question):
        items.pop()
    out = []
    for it in items:
        text = it['message']
        if it['role'] == 'assistant':
            text = _SOURCES_BLOCK_RE.sub('', text).strip()
            if not text:
                continue
            out.append({'role': 'assistant', 'message': text})
        else:
            out.append({'role': 'user', 'message': text})
    return out


def _context_search_query(history, question, max_questions=2):
    """Поисковый запрос с учётом диалога: последние вопросы пользователя + текущий.

    None — истории нет: поиск идёт по исходному вопросу, поведение системы не меняется.
    """
    if not history:
        return None
    try:
        n = max(1, int(max_questions))
    except (TypeError, ValueError):
        n = 2
    prev = [h['message'] for h in history if h.get('role') == 'user'][-n:]
    if not prev:
        return None
    joined = ' '.join(prev + [question or '']).strip()
    return joined or None


class RAGSettings:
    """Класс для управления настройками RAG-системы.
    Настройки хранятся в PostgreSQL (таблица app_settings). Файл rag_settings.json
    упразднён; параметры подключения к БД — в db_config.json (gitignored)."""
    def __init__(self):
        self.settings_file = None  # файла настроек больше нет — всё в БД
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
            # Память диалога: история уходит в messages, а поиск по БЗ при промахе
            # повторяется по склейке последних вопросов (вариант B, без доп. LLM-вызова)
            "chat_memory_enabled": True,         # веб-чат
            "chat_memory_external": True,        # виджет на сайте и чат-бот MAX
            "chat_memory_max_messages": 20,      # сколько последних сообщений берём в промпт
            "chat_memory_max_chars": 4000,       # бюджет символов на историю в промпте
            "chat_memory_ttl_minutes": 120,      # разрыв: старше N минут от последнего — не берём
            "chat_memory_context_questions": 2,  # сколько прошлых вопросов склеивать для поиска
            # Настройки OCR для сканированных PDF (DashScope qwen-vl-ocr)
            "ocr_enabled": False,
            "ocr_model": "qwen-vl-ocr",
            "ocr_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "ocr_dpi": 150
        }
        self.settings = self.load_settings()
    
    def load_settings(self):
        """Загрузка настроек из БД (таблица app_settings). При ошибке — defaults."""
        try:
            from auth_db import get_all_settings
            settings = get_all_settings()
            # Объединяем с настройками по умолчанию
            for key, value in self.default_settings.items():
                if key not in settings:
                    settings[key] = value
            return settings
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек из БД: {e}")
            return self.default_settings.copy()
    
    def save_settings(self):
        """Сохранение настроек в БД (UPSERT в app_settings)."""
        try:
            from auth_db import set_settings
            if set_settings(self.settings):
                logger.info("Настройки сохранены в БД")
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек в БД: {e}")
    
    def get(self, key, default=None):
        """Получение значения настройки"""
        return self.settings.get(key, default)

    def reload(self):
        """Перечитать настройки из БД (изменения из лаунчера применяются без рестарта
        веб-сервера). При ошибке — оставить текущие настройки, НЕ сбрасывать на defaults."""
        try:
            from auth_db import get_all_settings
            settings = get_all_settings()
            for key, value in self.default_settings.items():
                if key not in settings:
                    settings[key] = value
            self.settings = settings
            logger.info("⚙️ Настройки перечитаны из БД")
        except Exception as e:
            logger.error(f"Ошибка перечитывания настроек: {e} — оставлены текущие")
    
    def get_llm_api_key(self):
        """Ключ API для LLM-провайдера: DeepSeek → llm_provider_api_key (fallback llm_api_key),
        OpenRouter → llm_openrouter_api_key (fallback llm_provider_api_key / llm_api_key),
        остальные (DashScope и т.п.) → llm_api_key (он же используется для OCR)."""
        provider = self.settings.get("llm_provider", "DeepSeek")
        if model_catalog.provider_is_local(provider):
            # Локальный сервер ключ не проверяет, но ПУСТАЯ строка роняет OpenAI()
            # с «Missing credentials» — отдаём заглушку (наружу она не уходит).
            return "local"
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

# === Общая модель эмбеддингов (синглтон на процесс) ===
# Модель multilingual-e5-large весит ~2 ГБ RAM — грузим один раз и делим между
# всеми пользователями. Каждый RAGCore хранит только свои знания/эмбеддинги/BM25.
_EMBEDDING_MODEL = None
_EMBEDDING_MODEL_LOCK = threading.Lock()


def parse_qa_pairs_file(file_path):
    """Парсит файл исправлений (newdatabase.csv) → список [вопрос, ответ].

    CSV с заголовком «Вопрос,Ответ» (utf-8-sig — переживает BOM от Excel).
    Ответ может содержать запятые и переносы строк — он в кавычках.
    """
    pairs = []
    if not file_path or not os.path.isfile(file_path):
        return pairs
    try:
        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                q = (row.get("Вопрос") or "").strip()
                a = (row.get("Ответ") or "").strip()
                if q or a:
                    pairs.append([q, a])
    except Exception as e:
        logger.error(f"❌ Ошибка чтения {file_path}: {e}")
    return pairs


def _get_embedding_model():
    """Возвращает общий экземпляр модели эмбеддингов (лениво, потокобезопасно)."""
    global _EMBEDDING_MODEL
    with _EMBEDDING_MODEL_LOCK:
        if _EMBEDDING_MODEL is None:
            logger.info("🧠 Загрузка модели...")
            _EMBEDDING_MODEL = SentenceTransformer('intfloat/multilingual-e5-large')
            logger.info("✅ Модель загружена")
        return _EMBEDDING_MODEL


# Порог «значимого» фрагмента текста при извлечении из PDF/DOCX.
# Всё, что короче, — подписи к рисункам, заголовки таблиц, колонтитулы — раньше
# отбрасывалось (порог был 50) вместе с осмысленными заголовками разделов:
# на реальной базе (48 PDF, 760 страниц) фильтр выбрасывал 4540 фрагментов против
# 3803 оставленных, причём 2/3 отброшенного лежало на страницах с обычным текстом.
MIN_FRAGMENT_CHARS = 20
# «На странице есть настоящий текстовый слой» — по этому признаку решается, нужен ли OCR.
# Порог держим ОТДЕЛЬНО от MIN_FRAGMENT_CHARS: у страниц-чертежей в текстовом слое только
# 50-символьный колонтитул, и если считать такие страницы «текстовыми», OCR перестанет
# вытаскивать текст с картинок.
OCR_TRIGGER_MIN_CHARS = 50
# Короткие фрагменты-повторы в пределах одного файла оставляем один раз
# («Руководство по монтажу и наладке» встречается в файле до 89 раз — это колонтитул,
# 89 одинаковых векторов только засоряют индекс).
DEDUPE_SHORT_MAX_CHARS = 100
# Нарезка длинных фрагментов: e5 (multilingual-e5-large) обрезает вход на 512 токенов.
# Замер на реальной базе (1473 фрагмента): медиана 3.04 симв./токен, у «плотных» таблиц ~2.0,
# худший случай 1.6 (короткие числовые куски). 1200 символов ≈ 395 токенов обычного русского —
# с запасом влезает в окно модели, а «хвосты» длинных кусков (были по 2700–5400 симв.)
# перестают быть невидимыми для семантического поиска.
CHUNK_MAX_CHARS = 1200

# Границы предложений: точка/!/?/… перед пробелом (пунктуация остаётся в предложении)
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+')


def _hard_wrap(text, max_chars):
    """Жёсткая нарезка по словам: каждый кусок ≤ max_chars (сверхдлинное слово режется)."""
    out, current = [], ''
    for word in text.split(' '):
        while len(word) > max_chars:
            if current:
                out.append(current)
                current = ''
            out.append(word[:max_chars])
            word = word[max_chars:]
        if not word:
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += ' ' + word
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out


def _split_into_pieces(text, max_chars):
    """Текст → куски ≤ max_chars: сначала по абзацам, потом по предложениям, затем жёстко."""
    pieces = []
    for paragraph in (text or '').split('\n'):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in (s.strip() for s in _SENTENCE_SPLIT_RE.split(paragraph)):
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                pieces.append(sentence)
            else:
                pieces.extend(_hard_wrap(sentence, max_chars))
    return pieces


def split_long_fragments(fragments, max_chars=CHUNK_MAX_CHARS):
    """Нарезка длинных фрагментов на куски, влезающие в окно модели эмбеддингов.

    Зачем: e5 обрезает вход на 512 токенов, поэтому у фрагментов по 2700–5400 символов
    (типично для DOCX, где куском становится целый раздел) «хвост» не участвовал ни в
    семантическом поиске, ни в выдаче. Режем по абзацам и предложениям, короткий
    префикс источника («[файл, стр. N] ») повторяем в каждом куске, чтобы цитата
    оставалась узнаваемой. Гарантия: каждый кусок вместе с префиксом ≤ max_chars —
    из-за этого предел считается за вычетом длины префикса, а слишком короткий
    последний кусок остаётся отдельным фрагментом (это факт, а не мусор).
    """
    out = []
    for fragment in fragments or []:
        text = (fragment or '').strip()
        if not text:
            continue
        if len(text) <= max_chars:
            out.append(text)
            continue
        prefix = ''
        match = re.match(r'^(\[[^\]]{1,200}\]\s*)', text)
        if match:
            prefix, text = match.group(1), text[match.end():]
        # Префикс источника повторяется в каждом куске — резервируем под него место,
        # чтобы кусок вместе с префиксом остался в пределах max_chars
        body_limit = max(200, max_chars - len(prefix))
        chunks = []
        for piece in _split_into_pieces(text, body_limit):
            if chunks and len(chunks[-1]) + 1 + len(piece) <= body_limit:
                chunks[-1] += ' ' + piece
            else:
                chunks.append(piece)
        # Короткий последний кусок остаётся отдельным фрагментом: упаковка выше уже склеила
        # всё, что влезало, значит этот кусок в предыдущий не помещается, а вылезать за предел
        # ради склейки нельзя (иначе семантика снова обрежет хвост).
        out.extend(prefix + chunk for chunk in chunks)
    return out


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
        self._state_lock = threading.RLock()  # защита reload/search от гонок внутри одного core
        self.qa_corrections = []  # пары Вопрос-Ответ из newdatabase.txt (исправления пользователя)
        
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
        """Инициализация модели эмбеддингов (общий синглтон на процесс — грузится один раз)."""
        try:
            self.model = _get_embedding_model()
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

    @staticmethod
    def _ocr_model_slug(model):
        """Имя модели → безопасный сегмент пути (правило — в model_catalog)."""
        return model_catalog.ocr_model_slug(model)

    def _get_ocr_cache_path(self, file_path, page_num):
        """Путь к кэшу OCR-текста страницы (по хэшу PDF-файла), чтобы не жечь деньги
        на повторных переиндексациях.

        Каталог разбит ПО МОДЕЛИ: текст, распознанный qwen-vl-ocr, нельзя отдавать
        как результат qwen-vl-plus — после смены ocr_model (в т.ч. из админки)
        страницы распознаются заново, а старый кэш остаётся на месте.
        """
        if self.current_user_id is not None:
            cache_dir = Path("embeddings_cache") / f"user_{self.current_user_id}" / "ocr_cache"
        else:
            cache_dir = Path("embeddings_cache") / "ocr_cache"
        file_hash = hashlib.md5(open(file_path, 'rb').read()).hexdigest()[:12]
        model_slug = self._ocr_model_slug(self.settings.get("ocr_model", "qwen-vl-ocr"))
        page_dir = cache_dir / file_hash / model_slug
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
            # Разбираем ответ аккуратно: на странице без текста (пустая форма, графика)
            # qwen-vl-ocr отдаёт 200, finish_reason=stop и message ВООБЩЕ БЕЗ поля content.
            # Прямой доступ ["content"] падал с KeyError и попадал в лог как «Ошибка OCR»,
            # а страница не кэшировалась — API вызывался заново при каждой загрузке базы.
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                logger.error(f"❌ OCR: ответ без choices (HTTP {resp.status_code}): {resp.text[:200]}")
                return None
            message = choices[0].get("message") or {}
            text = message.get("content")
            if isinstance(text, list):     # часть моделей отдаёт content списком частей
                text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict))
            text = (text or "").strip()
            if not text:
                logger.info(f"ℹ️ OCR не распознал текст на странице {page_num+1} ({base_name}) — "
                            f"пустая или графическая страница; повторно не запрашиваем")
                cache_path.write_text("", encoding='utf-8')   # маркер: не тратить API-вызовы снова
                return None
            cache_path.write_text(text, encoding='utf-8')
            return text
        except Exception as e:
            logger.error(f"❌ Ошибка OCR страницы {page_num+1} ({base_name}): {e}")
            return None

    def _page_fragments(self, paragraphs):
        """Отбор фрагментов страницы из сырых блоков текста.

        Возвращает (fragments, long_chars):
          * fragments — тексты, пригодные для базы знаний (> MIN_FRAGMENT_CHARS и не мусор),
            в исходном порядке;
          * long_chars — сколько символов дал «настоящий» текстовый слой. По этому числу
            решается, нужен ли OCR: короткие колонтитулы и подписи в базе теперь есть,
            но страницу-чертёж они «текстовой» не делают.

        Многострочные блоки склеиваются в один пробел (для PDF это абзац).
        """
        fragments, long_chars = [], 0
        for paragraph in paragraphs or []:
            paragraph = " ".join(line.strip() for line in (paragraph or "").split('\n') if line.strip())
            if not paragraph or self._is_garbage_text(paragraph):
                continue
            if len(paragraph) > OCR_TRIGGER_MIN_CHARS:
                long_chars += len(paragraph)
            if len(paragraph) > MIN_FRAGMENT_CHARS:
                fragments.append(paragraph)
        return fragments, long_chars

    @staticmethod
    def _dedupe_key(paragraph):
        """Ключ дедупликации коротких фрагментов (None — длинные не дедуплицируем).

        Короткие блоки («Руководство по монтажу и наладке», «Аэрационная колонна | Паспорт»)
        повторяются на каждой странице: один такой вектор в индексе полезен, десятки
        одинаковых — шум. Длинные абзацы оставляем: одинаковый текст в разных местах
        документа обычно несёт разный контекст.
        """
        if len(paragraph) > DEDUPE_SHORT_MAX_CHARS:
            return None
        return ' '.join(paragraph.lower().split())

    def _extract_pdf_text(self, file_path):
        """Извлечение текста из PDF: PyMuPDF (качественный) с фолбэком на PyPDF2.
        Пустые/содержащие только картинки страницы распознаются через OCR, если он включён.
        Возвращает список фрагментов с префиксом источника."""
        base_name = os.path.basename(file_path)
        pdf_knowledge = []
        seen_short = set()   # короткие фрагменты, уже добавленные из этого файла
        ocr_enabled = bool(self.settings.get("ocr_enabled", False))

        def add_fragment(paragraph, page_num):
            """Добавить фрагмент страницы, отсекая повторы коротких (колонтитулы)."""
            key = self._dedupe_key(paragraph)
            if key is not None:
                if key in seen_short:
                    return False
                seen_short.add(key)
            pdf_knowledge.append(f"[{base_name}, стр. {page_num}] {paragraph}")
            return True

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(file_path)
            try:
                logger.info(f"📄 Обработка PDF (PyMuPDF): {base_name} (всего страниц: {len(doc)})")
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    # blocks + sort=True: правильный порядок чтения и отсев дублей
                    # текстового слоя (дизайнерские PDF и буклеты часто дублируют текст)
                    page_fragments, long_chars = self._page_fragments(
                        [block[4] for block in page.get_text("blocks", sort=True)])

                    # OCR нужен, когда текстового слоя на странице нет (скан или чертёж).
                    # Признак — отсутствие ДЛИННЫХ блоков, а не отсутствие фрагментов:
                    # короткие колонтитулы и подписи теперь тоже идут в базу, и по ним
                    # нельзя считать страницу «текстовой» (иначе потеряем текст с картинок).
                    if long_chars == 0 and ocr_enabled:
                        cache_path = self._get_ocr_cache_path(file_path, page_num)
                        ocr_text = self._ocr_page(page, base_name, page_num, cache_path)
                        if ocr_text:
                            ocr_fragments, _ = self._page_fragments(ocr_text.split('\n'))
                            page_fragments.extend(ocr_fragments)

                    for paragraph in page_fragments:
                        add_fragment(paragraph, page_num + 1)
            finally:
                doc.close()
        except ImportError:
            # Фолбэк: старый PyPDF2 (если PyMuPDF не установлен)
            logger.info(f"📄 Обработка PDF (PyPDF2 fallback): {base_name}")
            with open(file_path, 'rb') as f:
                pdf_reader = PyPDF2.PdfReader(f)
                for page_num, page in enumerate(pdf_reader.pages):
                    text = page.extract_text()
                    if not text:
                        continue
                    page_fragments, _ = self._page_fragments(text.split('\n\n'))
                    for paragraph in page_fragments:
                        add_fragment(paragraph, page_num + 1)
        return pdf_knowledge

    def _process_file(self, file_path, all_knowledge):
        """Обработка одного файла"""
        try:
            if file_path.lower().endswith(".txt"):
                with open(file_path, "r", encoding="utf-8") as file:
                    # Строки-заголовки («# Раздел», «# Сайт — https://...») в индекс не идут:
                    # это разметка файла для человека, а не факт для базы знаний.
                    # Формат БЗ их разрешает (см. навык rag-knowledge-base-formatting),
                    # поэтому фильтр обязан быть здесь — иначе заголовки висят в поиске мусором.
                    lines = [line.strip() for line in file
                             if line.strip() and not line.lstrip().startswith('#')]
                    if lines:
                        logger.info(f"✅ Загружено {len(lines)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = lines
                    else:
                        logger.warning(f"⚠️ Файл {os.path.basename(file_path)} пуст")
                        
            elif file_path.lower().endswith(".csv"):
                # Автодетект разделителя (';' — выгрузки 1С/CRM) и кодировки (utf-8/cp1251).
                # Без этого ';'-файлы читаются как один столбец, а лишние поля строки
                # попадают под ключ None и роняют обработку (None.strip()).
                rows = []
                for enc in ("utf-8-sig", "cp1251"):
                    try:
                        with open(file_path, "r", encoding=enc, newline="") as csvfile:
                            sample = csvfile.read(8192)
                            csvfile.seek(0)
                            try:
                                delim = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
                            except csv.Error:
                                delim = ","
                            reader = csv.DictReader(csvfile, delimiter=delim)
                            rows = list(reader)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                csv_knowledge = []
                for row in rows:
                    parts = []
                    for key, value in row.items():
                        if not key:  # None-ключ: лишние поля строки (restkey) — пропускаем
                            continue
                        clean_key = str(key).strip()
                        if clean_key.lower().startswith("unnamed"):
                            continue
                        if isinstance(value, list):  # restkey собирает лишние поля в список
                            value = " ".join(str(v) for v in value if v)
                        vs = str(value).strip() if value is not None else ""
                        if vs:
                            parts.append(f"{clean_key} — {vs}")
                    # Одна строка CSV = один факт (универсально, без привязки к колонкам клиента)
                    if parts:
                        csv_knowledge.append(", ".join(parts) + ".")
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
                    
                    # Порог общий с PDF (MIN_FRAGMENT_CHARS): в спецификациях половина
                    # полезного — короткие ячейки вида «раструб d32 под склейку»
                    docx_seen_short = set()
                    for paragraph in paragraphs:
                        if len(paragraph) <= MIN_FRAGMENT_CHARS:  # Фильтруем короткие фрагменты
                            continue
                        key = self._dedupe_key(paragraph)
                        if key is not None:
                            if key in docx_seen_short:
                                continue
                            docx_seen_short.add(key)
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
                                    # Одна строка = один факт (универсально, без привязки к колонкам клиента)
                                    entry = f"[{os.path.basename(file_path)}, лист '{sheet_name}'] " + ", ".join(parts) + "."
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
                old_embeddings = self._load_cache_pickle(latest_old_cache)
                
                # Проверяем формат
                if isinstance(old_embeddings, dict) and 'embeddings' in old_embeddings:
                    embeddings = old_embeddings['embeddings']
                    old_hash = old_embeddings.get('hash', 'unknown')
                else:
                    # Старый формат — просто эмбеддинги
                    embeddings = old_embeddings
                    old_hash = 'old_format'
                if isinstance(embeddings, torch.Tensor):
                    embeddings = embeddings.to(self.model.device)
                
                logger.info(f"✅ Кэш-файл мигрирован: {latest_old_cache.name} -> {filename}.pkl")
                
                # Удаляем старый файл
                latest_old_cache.unlink()
                logger.info(f"🗑️ Старый кэш-файл удалён: {latest_old_cache.name}")
                
                # Возвращаем эмбеддинги (они будут пересохранены в новом формате)
                return embeddings, old_hash
            except Exception as e:
                logger.error(f"⚠️ Ошибка при миграции кэша {latest_old_cache}: {e}")
        
        return None

    def _load_cache_pickle(self, cache_path):
        """Читает pkl-кэш эмбеддингов, перенося тензоры на CPU.

        Кэш может быть создан на машине с GPU (например, сервер Чеба): внутри
        лежат CUDA-тензоры, и обычный pickle.load на CPU-only машине падает
        («Attempting to deserialize object on a CUDA device...»). Файлы кэша
        создаются pickle.dump (не torch.save), поэтому даже torch.load с
        map_location='cpu' не пробрасывает map_location во внутренний
        _load_from_bytes (torch 2.13). Поэтому перехватываем
        torch.storage._load_from_bytes кастомным Unpickler'ом и читаем байты
        хранилища сразу на CPU.
        """
        class _CudaSafeUnpickler(pickle.Unpickler):
            def find_class(self, module, name):
                if module == 'torch.storage' and name == '_load_from_bytes':
                    return lambda b: torch.load(
                        io.BytesIO(b), map_location='cpu', weights_only=False)
                return super().find_class(module, name)

        with open(cache_path, 'rb') as f:
            try:
                return _CudaSafeUnpickler(f).load()
            except Exception:
                pass
        with open(cache_path, 'rb') as f:
            try:
                return torch.load(f, map_location='cpu', weights_only=False)
            except Exception:
                pass
        with open(cache_path, 'rb') as f:
            return pickle.load(f)

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
                cached_data = self._load_cache_pickle(cache_path)
                
                # Проверяем структуру данных (поддержка старых кэшей без хэша)
                if isinstance(cached_data, dict) and 'hash' in cached_data and 'embeddings' in cached_data:
                    cached_hash = cached_data['hash']
                    embeddings = cached_data['embeddings']
                    if isinstance(embeddings, torch.Tensor):
                        embeddings = embeddings.to(self.model.device)
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
        """Перезагрузка базы знаний (потокобезопасно: блокирует поиск на время пересборки)."""
        with self._state_lock:
            return self._reload_knowledge_base_impl(user_id)

    def _reload_knowledge_base_impl(self, user_id=None):
        """Перезагрузка базы знаний"""
        try:
            logger.info("\n🔄 Перезагрузка базы знаний...")
            
            # Загрузка знаний
            self.all_knowledge_dict = self.load_knowledge_from_txt(user_id=user_id)
            # Нарезка длинных фрагментов ДО эмбеддингов: e5 обрезает вход на 512 токенов,
            # из-за чего «хвост» кусков по 2700–5400 символов не искался вообще. Режем один
            # раз здесь — my_knowledge, fragment_sources и кэш эмбеддингов идут по одному
            # и тому же all_knowledge_dict, поэтому соответствие индекс↔источник сохраняется.
            self.all_knowledge_dict = {
                path: split_long_fragments(items)
                for path, items in self.all_knowledge_dict.items()
            }
            self._load_qa_corrections()
            
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

    def _load_qa_corrections(self):
        """Загрузка исправлений Вопрос-Ответ из newdatabase.csv пользователя.

        Вызывается при каждой пересборке БЗ. Эмбеддинги вопросов считаются заранее,
        чтобы при ответе не гонять модель на каждый вопрос.
        """
        self.qa_corrections = []
        if self.current_user_id is None:
            return
        path = os.path.join("Database", f"user_{self.current_user_id}", QA_CORRECTION_FILE)
        pairs = [p for p in parse_qa_pairs_file(path) if p[0].strip() and p[1].strip()]
        if not pairs:
            return
        try:
            q_embs = self.model.encode([q for q, a in pairs], convert_to_tensor=True)
            if q_embs.dim() == 1:
                q_embs = q_embs.unsqueeze(0)
            self.qa_corrections = [
                {"question": q, "answer": a, "embedding": emb}
                for (q, a), emb in zip(pairs, q_embs)
            ]
            logger.info(f"✅ Загружено исправлений Вопрос-Ответ: {len(self.qa_corrections)} ({QA_CORRECTION_FILE})")
        except Exception as e:
            logger.error(f"❌ Ошибка эмбеддинга исправлений {QA_CORRECTION_FILE}: {e}")

    def _try_qa_correction(self, question):
        """Вариант B: ищем вопрос среди сохранённых исправлений пользователя.

        Три уровня:
          1) точное совпадение после нормализации (регистр/пробелы) → ответ однозначно;
          2) иначе косинусная схожесть: best ≥ порог И, если исправлений несколько,
             отрыв от второго лучшего ≥ маржи (e5 даёт высокий фон для любых вопросов);
          3) иначе None → обычный RAG-путь (фрагменты newdatabase.csv всё равно в БЗ).
        """
        if not self.qa_corrections or not question:
            return None
        try:
            norm_q = " ".join(question.lower().split())
            for c in self.qa_corrections:
                if " ".join(c["question"].lower().split()) == norm_q:
                    logger.info("✅ Исправление: точное совпадение вопроса")
                    return self._format_correction_answer(c["answer"])

            q_emb = self.model.encode(question, convert_to_tensor=True)
            matrix = torch.stack([c["embedding"] for c in self.qa_corrections])
            scores = util.cos_sim(q_emb, matrix)[0]
            best_idx = int(scores.argmax())
            best_score = float(scores[best_idx])
            threshold = self.settings.get("qa_correction_threshold", QA_CORRECTION_THRESHOLD)
            if best_score < threshold:
                logger.info(f"ℹ️ Исправление не применено: макс. схожесть {best_score:.3f} < {threshold}")
                return None
            if len(self.qa_corrections) > 1:
                sorted_scores, _ = torch.sort(scores, descending=True)
                margin = float(sorted_scores[0] - sorted_scores[1])
                margin_need = self.settings.get("qa_correction_margin", QA_CORRECTION_MARGIN)
                if margin < margin_need:
                    logger.info(f"ℹ️ Исправление не применено: маржа {margin:.3f} < {margin_need}")
                    return None
            corr = self.qa_corrections[best_idx]
            logger.info(f"✅ Применено исправление (схожесть {best_score:.3f}): {corr['question']}")
            return self._format_correction_answer(corr["answer"])
        except Exception as e:
            logger.error(f"❌ Ошибка поиска исправления: {e}")
            return None

    def _format_correction_answer(self, answer):
        """Исправленный ответ + ссылка на источник newdatabase.csv (или None)."""
        answer = (answer or "").strip()
        if not answer:
            return None
        return answer + "\n\n" + self._format_sources_note([QA_CORRECTION_FILE])

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

    def find_relevant_info(self, query, top_k=None, alpha=None, alt_query=None):
        """Гибридный поиск (потокобезопасно: читает согласованный снимок БЗ).

        alt_query — тот же вопрос, переформулированный с учётом диалога (прошлые вопросы
        пользователя + текущий). Если по исходному запросу релевантность ниже порога,
        повторяем поиск по нему: follow-up вроде «а второй сколько стоит?» без этого
        находит пустоту, хотя тема была названа в предыдущем сообщении.
        """
        with self._state_lock:
            text, files, score = self._search_once(query, top_k, alpha)
            if alt_query and alt_query.strip() != (query or '').strip():
                threshold = self.settings.get("relevance_threshold", 0.2)
                if score < threshold:
                    logger.info(f"🔁 Повторный поиск с учётом диалога: \"{alt_query}\"")
                    text2, files2, score2 = self._search_once(alt_query, top_k, alpha)
                    if score2 > score:
                        return text2, files2
            return text, files

    def _search_once(self, query, top_k=None, alpha=None):
        """Один проход гибридного поиска: (контекст, файлы-источники, лучший скор)."""
        # Используем значения из настроек, если не переданы другие
        actual_top_k = top_k if top_k is not None else self.settings.get("search_top_k", 10)
        actual_alpha = alpha if alpha is not None else self.settings.get("search_alpha", 0.7)
        relevance_threshold = self.settings.get("relevance_threshold", 0.2)
        max_context_fragments = self.settings.get("max_context_fragments", 100)
        
        # Проверяем настройку отключения гибридного поиска
        if self.settings.get("disable_hybrid_search", False):
            logger.info("🔤 Гибридный поиск отключен. Передаем всю базу знаний.")
            indices = list(range(min(len(self.my_knowledge), max_context_fragments)))
            return "\n".join(self.my_knowledge[:max_context_fragments]), self._get_source_files(indices), 1.0
        
        logger.info(f"\n🔍 Поиск релевантной информации для запроса: \"{query}\"")
        
        if not self.my_knowledge or self.corpus_embeddings is None or self.bm25 is None:
            logger.warning("⚠️ База знаний не загружена.")
            return "База знаний не загружена.", [], 0.0

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
            return "Я не знаю ответа на этот вопрос.", [], best_score

        result = [self.my_knowledge[idx] for idx in top_indices]
        source_files = self._get_source_files(top_indices)
        logger.info(f"✅ Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
        return "\n".join(result), source_files, best_score
    
    def ask_model(self, question, user_prompt=None, history=None):
        """Отправка запроса ко всем доступным моделям

        user_prompt: персональный системный промт пользователя (если задан — используется вместо системного по умолчанию)
        history: предыдущие сообщения диалога [{'role','message'}] — их даёт канал (веб-чат,
            виджет, MAX). Модель видит их как обычные ходы, а поиск по базе знаний при
            низкой релевантности повторяется по склейке прошлых вопросов с текущим (вариант B).
        """
        # Перечитываем настройки из файла — изменения из лаунчера применяются без рестарта сервера
        self.settings.reload()
        # Обновляем клиент с актуальными настройками
        self._setup_client()

        # Память диалога: чистим пришедшую историю и готовим поисковый запрос с её учётом
        history = _prepare_chat_history(history, question)
        ctx_query = _context_search_query(history, question,
                                          self.settings.get("chat_memory_context_questions", 2))
        if history:
            logger.info(f"🧠 Память диалога: {len(history)} сообщ. (поисковый запрос: \"{ctx_query}\")")
        else:
            ctx_query = None

        # Точный путь: ценовой вопрос → поиск по прайс-листу (PostgreSQL).
        # Если найдено — отвечаем фактами из таблицы без LLM.
        price_answer = self._try_price_answer(question)
        if not price_answer and ctx_query:
            # Follow-up про цену («а второй сколько стоит?») — ищем по склейке с прошлым вопросом
            price_answer = self._try_price_answer(ctx_query)
        if price_answer:
            if self.settings.get("disable_llm_models", False):
                price_answer += "\n\n⚠️ Отправка запросов к LLM моделям отключена."
            return price_answer
        
        # Вариант B: пользователь исправил ответ → пара Вопрос-Ответ в newdatabase.txt.
        # Похожий вопрос → отвечаем исправлением напрямую (без LLM и поиска по БЗ).
        if not self.settings.get("disable_qa_corrections", False):
            correction_answer = self._try_qa_correction(question)
            if not correction_answer and ctx_query:
                correction_answer = self._try_qa_correction(ctx_query)
            if correction_answer:
                return correction_answer

        # Проверяем настройку отключения поиска в базе знаний
        disable_kb_search = self.settings.get("disable_knowledge_base_search", False)
        
        context = ""
        source_files = []
        
        if not disable_kb_search:
            # Получаем настройки поиска
            search_top_k = self.settings.get("search_top_k", 10)
            search_alpha = self.settings.get("search_alpha", 0.7)
            
            context_result = self.find_relevant_info(question, top_k=search_top_k, alpha=search_alpha,
                                                     alt_query=ctx_query)
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
        
        # История диалога — между системным промтом и текущим вопросом
        messages = [{"role": "system", "content": system_prompt}]
        messages += [{"role": h["role"], "content": h["message"]} for h in history]
        messages.append({"role": "user", "content": question})

        # Единственная активная модель — из настроек (раньше был цикл по AVAILABLE_MODELS,
        # но список содержал одну модель и только путал: фактически всегда бралась llm_model).
        active_model = self.settings.get("llm_model", "deepseek-v4-flash")
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
                    "messages": messages,
                },
                timeout=60,
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"].strip()
            answers.append(answer)
            logger.info(f"✅ Ответ получен от модели {active_model}")
        except Exception as e:
            error_msg = f"❌ Ошибка при обращении к модели {active_model}: {e}"
            # На Windows str(e) может содержать символы, ломающие ascii-кодировку
            error_msg_safe = error_msg.encode('utf-8', errors='replace').decode('utf-8')
            logger.error(error_msg_safe)
            answers.append(f"Ответ ИИ модели {active_model}:\nОшибка: {error_msg_safe}\n")

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


# === Реестр per-user экземпляров (изоляция пользователей, потокобезопасно) ===
# Один RAGCore на пользователя: БЗ/эмбеддинги/BM25 одного пользователя не затирают
# другого при параллельных запросах. Модель эмбеддингов — общий синглтон
# (см. _get_embedding_model), поэтому создание новых core дешёвое.
_user_cores = {}
_user_cores_lock = threading.Lock()


def get_user_rag(user_id):
    """Возвращает RAGCore конкретного пользователя (лениво создаётся и кэшируется)."""
    with _user_cores_lock:
        core = _user_cores.get(user_id)
        if core is None:
            core = RAGCore(user_id=user_id)
            _user_cores[user_id] = core
        return core


def drop_user_rag(user_id):
    """Выгрузить RAGCore пользователя из памяти (нужно при удалении пользователя).

    Освобождает эмбеддинги/BM25/фрагменты БЗ — у активных пользователей это
    гигабайты RAM. Общую модель эмбеддингов (синглтон) не трогаем.
    """
    with _user_cores_lock:
        core = _user_cores.pop(user_id, None)
    if core is None:
        return
    try:
        core.my_knowledge = []
        core.all_knowledge_dict = {}
        core.fragment_sources = []
        core.corpus_embeddings = None
        core.bm25 = None
        core.tokenized_corpus = None
        logger.info(f"🧹 RAGCore пользователя #{user_id} выгружен из памяти")
    except Exception as e:
        logger.error(f"Ошибка выгрузки RAGCore пользователя #{user_id}: {e}")

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