# -*- coding: utf-8 -*-
"""Сценарий «ответ на отзыв»: определение задачи, типа претензии и примеров ответов.

Зачем отдельный слой. На задачу вида «ответь на отзыв <товар> + текст отзыва» обычный
RAG-поиск приносит фрагменты каталога (характеристики, комплектация), а примеры ответов
компании («…Отзывы - Sheet1.csv», «Позитивные ответы на отзывы.txt») в топ не попадают.
Из-за этого первый ответ выходит казённой отпиской («все товары сертифицированы, брак —
крайне редкое исключение»), а правильная процедура (уточнить дефект, попросить фото)
появляется только после того, как сотрудник напишет её руками.

Здесь сценарий определяется ДЕТЕРМИНИРОВАННО по тексту задачи, тип претензии — по лексике,
и в контекст первым блоком уходят чеклист ответа + 2–3 примера того же типа. Модель не
дообучается: каждый раз она получает процедуру и образцы стиля из своей же базы знаний.

Ничего не хранится в БД: примеры — файлы базы знаний пользователя, поэтому правка
«Отзывы.csv» меняет поведение сразу (кэш — по mtime/размеру файла, см. load_examples).
"""

import csv
import io
import os
import re
import threading
from difflib import SequenceMatcher

# --- Типы претензий ---------------------------------------------------------
# Порядок важен: проверяем сверху вниз, первое совпадение выигрывает.
# «повреждение» раньше «доставки» — «разбит при транспортировке» это про товар,
# «не подошёл» раньше «брака» — «не подходит по диаметру» это не дефект.
NEGATIVE_TYPES = ("повреждение", "доставка", "не подошёл", "брак", "цена", "обслуживание")

_COMPLAINT_RULES = (
    ("повреждение", re.compile(
        r"повреж|разбит|треснул|погнут|вмятин|помят|порван|раздавл|разломил|"
        r"потёк при доставк|потек при доставк", re.I)),
    ("доставка", re.compile(
        r"доставк|курьер|транспортн|опозда|задерж|не привез|потерял|трек|"
        r"пункт выдачи|ждал[аи]? (?:нед|долго|месяц)|не пришл[ао] (?:вовремя|в срок)", re.I)),
    ("не подошёл", re.compile(
        r"не подошл|не подходит|не подход|не тот товар|пересорт|другой товар|"
        r"не соответству|не совместим|не встал|не влез|ошиблись с|"
        r"не тот размер|не тот артикул|не ту модель|не та модель|"
        r"вместо .{0,40}привез|привезли не то|пришл[ао] не то", re.I)),
    ("брак", re.compile(
        r"брак|не работ|нерабоч|сломал|дефект|течёт|течет|протека|потёк|потек|"
        r"не держ|не выполня|на выброс|выброс|мусор|отказал", re.I)),
    ("цена", re.compile(
        r"дорог|переплат|завыш|цена высок|стоимость высок|скидк|дешевл|обдирал", re.I)),
    ("обслуживание", re.compile(
        r"менеджер|сотрудник|персонал|хамств|грубост|не отвеча|игнорир|поддержк|"
        r"не перезвон", re.I)),
)

_POSITIVE = re.compile(
    r"спасибо|благодар|доволен|довольн|отличн|рекомендую|супер|качественн|понравил|"
    r"всё хорошо|все хорошо|быстро пришл|удобн", re.I)
# «не понравилось», «не рекомендую» — это не положительный отзыв
_NEGATED_POSITIVE = re.compile(r"\bне\s+(?:очень\s+)?(?:понрав|рекоменд|довол)", re.I)

# --- Признаки задачи «написать ответ на отзыв» ------------------------------
_REVIEW_MARKERS = re.compile(r"отзыв|рецензи", re.I)
_REVIEW_WORDS = re.compile(r"претензи|жалоб", re.I)
_TASK_VERB = re.compile(
    r"ответь|ответить|напиши|написать|составь|составить|сформулируй|подготовь|"
    r"придумай|отредактируй|поправь", re.I)
_CLIENT_WORD = re.compile(r"клиент|покупател|заказчик|гост", re.I)
_REVIEW_STRUCT = re.compile(r"достоинств|недостатк|комментарий\s*:|оценка\s*:|отзыв\s*:", re.I)
# Клиентский текст от первого лица: «купил… пришлось выбросить», «верните деньги».
# Нужен, когда сотрудник вставляет отзыв вообще без слова «отзыв».
_CLIENT_VOICE = re.compile(
    r"купил|заказал|получил|пришлось|верните|деньги на ветер|разочарован|"
    r"не рекомендую|пришёл|пришел", re.I)

# --- Оценка качества примера ------------------------------------------------
# Хороший ответ на претензию: благодарит/извиняется и задаёт следующий шаг
# (уточняющий вопрос, просьба о фото, номер заказа). Такие примеры идём вперёд.
_GOOD_CUES = re.compile(
    r"уточните|уточнить|пришлите|прислать|вышлите|подскажите|фото|видео|"
    r"номер заказа|что именно|в чём именно|в чем именно|опишите|подробн|"
    r"спасибо|извин|благодар", re.I)
_BAD_CUES = re.compile(
    r"не является браком|соответствует по своим техническим характеристикам|"
    r"вы неправильно|согласно инструкции по эксплуатации", re.I)

REVIEW_CSV_HINTS = ("отзыв", "review")
REVIEW_TXT_HINTS = ("отзыв", "review")
_MAX_ANSWER_CLIP = 700      # длина одного примера-ответа в блоке
_MAX_COMPLAINT_CLIP = 400   # длина текста отзыва в блоке
_MAX_BLOCK_CHARS = 5200     # общий бюджет блока (промт не раздуваем)


def _norm(text):
    return " ".join((text or "").lower().split())


def clip(text, limit):
    """Обрезка по границе слова с многоточием."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def classify_complaint(text):
    """Тип претензии по тексту отзыва/задачи.

    Возвращает один из NEGATIVE_TYPES, «положительный» или «общий».
    """
    text = text or ""
    for label, rx in _COMPLAINT_RULES:
        if rx.search(text):
            return label
    if _POSITIVE.search(text) and not _NEGATED_POSITIVE.search(text):
        return "положительный"
    return "общий"


def detect_review_task(text):
    """Является ли сообщение задачей «написать ответ на отзыв».

    Возвращает {"type": <тип претензии>, "signals": [...]} или None.
    Сознательно консервативно: сомнительные случаи не детектим — тогда работает
    обычный RAG-путь, как раньше.
    """
    text = (text or "").strip()
    if not text:
        return None
    ctype = classify_complaint(text)
    is_negative = ctype in NEGATIVE_TYPES
    signals = []

    if _REVIEW_MARKERS.search(text):
        signals.append("слово-отзыв")
    if _REVIEW_WORDS.search(text):
        signals.append("претензия/жалоба")
    if _REVIEW_STRUCT.search(text):
        signals.append("структура-отзыва")
    if _TASK_VERB.search(text) and _CLIENT_WORD.search(text) and (is_negative or ctype == "положительный"):
        signals.append("задача-ответ-клиенту")
    if is_negative and _CLIENT_VOICE.search(text) and "?" not in text:
        signals.append("текст-от-клиента")

    if not signals:
        return None
    return {"type": ctype, "signals": signals}


# --- Чеклисты по типам претензии -------------------------------------------

_COMMON_FORBIDDEN = (
    "«строгий контроль качества», «все товары сертифицированы», «брак — крайне редкое "
    "исключение»: для клиента это звучит как недоверие и отписка",
    "спорить с клиентом и перекладывать вину («вы неправильно эксплуатировали») без доказательств",
    "обещать возврат/замену до того, как выяснены обстоятельства",
    "выдумывать характеристики, сроки и условия, которых нет в информации",
    "отвечать одной фразой «оставьте контакты»: сначала уточняющие вопросы, потом контакты",
)

_STEPS = {
    "брак": (
        "Поблагодарить за отзыв и сразу признать проблему, без оправданий.",
        "Сказать, что разберёмся в причине (по описанию и фото).",
        "Уточнить, в чём именно дефект: не включается, течёт, не держит давление, треснул.",
        "Попросить фото товара и номер заказа.",
        "Предложить решение: диагностика, замена или возврат — и контакт для связи.",
    ),
    "повреждение": (
        "Извиниться за то, что товар пришёл повреждённым.",
        "Уточнить характер повреждения (разбит, помят, треснул, порвана упаковка).",
        "Попросить фото товара и упаковки, номер заказа.",
        "Предложить замену или компенсацию и назвать сроки рассмотрения.",
    ),
    "доставка": (
        "Извиниться за задержку и неудобства.",
        "Уточнить номер заказа и способ доставки.",
        "Объяснить статус или проверить его и назвать реальный срок.",
        "Предложить, что делаем дальше, и оставить контакт.",
    ),
    "не подошёл": (
        "Поблагодарить за отзыв.",
        "Уточнить модель оборудования и параметры, для которых товар не подошёл.",
        "Объяснить, чем отличается их случай, без поучений.",
        "Предложить подходящий вариант или обмен и попросить номер заказа.",
    ),
    "цена": (
        "Поблагодарить и не спорить с оценкой цены.",
        "Объяснить, из чего складывается стоимость (комплектация, производство, гарантия, сервис).",
        "Предложить альтернативу дешевле или условия для опта.",
        "Оставить контакт для расчёта.",
    ),
    "обслуживание": (
        "Извиниться за опыт общения.",
        "Уточнить, когда и с кем был контакт (имя менеджера, дата, канал).",
        "Сказать, что разберёмся и вернёмся с ответом.",
        "Дать контакт руководителя или прямой номер.",
    ),
    "положительный": (
        "Поблагодарить за отзыв — коротко и искренне.",
        "Отметить конкретику из отзыва (что именно понравилось), если она есть.",
        "Не продавать в ответе и не добавлять длинных описаний товара.",
        "Пригласить обращаться снова — одной фразой.",
    ),
    "общий": (
        "Поблагодарить за отзыв.",
        "Уточнить детали, которых не хватает для решения.",
        "Предложить конкретный следующий шаг.",
        "Оставить контакт для связи.",
    ),
}

_TYPE_TITLES = {
    "брак": "брак / товар не работает",
    "повреждение": "повреждение товара",
    "доставка": "доставка и сроки",
    "не подошёл": "товар не подошёл",
    "цена": "цена",
    "обслуживание": "обслуживание",
    "положительный": "положительный отзыв",
    "общий": "без явного типа",
}


# --- Чтение файлов с примерами ---------------------------------------------

def _read_text(path):
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def find_review_files(user_dir):
    """Файлы примеров ответов на отзывы в базе знаний пользователя.

    Ищем по имени («…отзыв…», «…review…»): .csv с колонками Товар/Отзыв/Ответ и
    .txt с готовыми фразами (банк положительных ответов). Папки .versions/lost+found
    и скрытые пропускаем — там бэкапы документов, а не актуальные примеры.
    """
    csv_paths, txt_paths = [], []
    if not user_dir or not os.path.isdir(user_dir):
        return csv_paths, txt_paths
    for dirpath, dirnames, filenames in os.walk(user_dir, onerror=lambda e: None):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in {".versions", "lost+found"})
        for fn in sorted(filenames):
            low = fn.lower()
            if fn.startswith("."):
                continue
            if not any(h in low for h in REVIEW_CSV_HINTS):
                continue
            if low.endswith(".csv"):
                csv_paths.append(os.path.join(dirpath, fn))
            elif low.endswith(".txt"):
                txt_paths.append(os.path.join(dirpath, fn))
    return csv_paths, txt_paths


def _parse_csv_rows(path):
    """Строки Отзыв→Ответ из CSV с гибкими названиями колонок."""
    text = _read_text(path)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return []
    if not rows:
        return []
    header = [(c or "").strip().lower() for c in rows[0]]

    def col(*needles):
        for i, h in enumerate(header):
            if any(n in h for n in needles):
                return i
        return None

    ci_prod = col("товар", "продукт", "наимено")
    ci_rev = col("отзыв", "комментар", "текст")
    ci_ans = col("ответ", "reply", "answer")
    if ci_rev is None or ci_ans is None:
        return []

    base = os.path.basename(path)
    out = []
    for row in rows[1:]:
        if len(row) <= max(ci_rev, ci_ans):
            continue
        answer = (row[ci_ans] or "").strip()
        complaint = (row[ci_rev] or "").strip()
        if len(answer) < 40 or not complaint:
            continue
        out.append({
            "product": (row[ci_prod] or "").strip() if ci_prod is not None and ci_prod < len(row) else "",
            "complaint": complaint,
            "answer": answer,
            "type": classify_complaint(complaint),
            "source": base,
        })
    return out


def _parse_txt_templates(path):
    """Готовые фразы по одной на строку (банк шаблонов)."""
    base = os.path.basename(path)
    out = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if len(line) < 30:
            continue
        out.append({"product": "", "complaint": "", "answer": line,
                    "type": "положительный", "source": base})
    return out


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def load_examples(user_dir):
    """Все примеры ответов на отзывы из базы знаний пользователя (с кэшем по mtime)."""
    csv_paths, txt_paths = find_review_files(user_dir)
    paths = list(csv_paths) + list(txt_paths)
    stamp = []
    for p in paths:
        try:
            st = os.stat(p)
            stamp.append((p, int(st.st_mtime), st.st_size))
        except OSError:
            continue
    key = (os.path.abspath(user_dir), tuple(stamp))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

    examples = []
    for p in csv_paths:
        try:
            examples.extend(_parse_csv_rows(p))
        except Exception:
            continue
    for p in txt_paths:
        # txt — банк коротких фраз; берём их только как «положительные», но не как
        # единственный источник, если в папке есть нормальный CSV с парами.
        try:
            examples.extend(_parse_txt_templates(p))
        except Exception:
            continue

    with _CACHE_LOCK:
        _CACHE.clear()          # держим один снимок: файлов немного, память не копим
        _CACHE[key] = examples
    return examples


def _quality(example):
    """Эвристика «пример хороший»: есть благодарность/вопрос к клиенту и нет отписок."""
    answer = example["answer"]
    score = len(set(m.group(0).lower() for m in _GOOD_CUES.finditer(answer)))
    score -= 2 * len(set(m.group(0).lower() for m in _BAD_CUES.finditer(answer)))
    return score


def select_examples(examples, ctype, max_n=3):
    """Подбор примеров: сначала тот же тип претензии, потом другие негативные.

    Похожие ответы отсеиваются: в базе часто лежит один и тот же шаблон на десятки
    отзывов, и три его копии в промте только съедают место без пользы.
    """
    if max_n <= 0:
        return []
    if ctype == "положительный":
        pool = [e for e in examples if e["type"] == "положительный"]
    else:
        same = [e for e in examples if e["type"] == ctype]
        other = [e for e in examples if e["type"] in NEGATIVE_TYPES and e["type"] != ctype]
        neutral = [e for e in examples if e["type"] == "общий"]
        pool = same + other + neutral

    pool = sorted(pool, key=lambda e: (-_quality(e), len(e["answer"])))
    picked, seen = [], set()
    for e in pool:
        key = _norm(e["answer"])[:160]
        if key in seen:
            continue
        if picked and _too_similar(e["answer"], picked):
            continue
        seen.add(key)
        picked.append(e)
        if len(picked) >= max_n:
            break
    return picked


def _too_similar(answer, picked, limit=0.62):
    """Почти дословный дубль уже выбранного примера (тот же шаблон ответа)."""
    a = _norm(answer)
    for e in picked:
        b = _norm(e["answer"])
        if SequenceMatcher(None, a, b).quick_ratio() < limit:
            continue
        if SequenceMatcher(None, a, b).ratio() >= limit:
            return True
    return False


def build_block(user_dir, task_text, max_examples=3):
    """Блок сценария для контекста модели: (текст, имена файлов-источников).

    Пустая строка — сообщение не похоже на задачу «ответ на отзыв» (или примеров нет
    и это не мешает: чеклист работает и без них).
    """
    detected = detect_review_task(task_text)
    if not detected:
        return "", []
    ctype = detected["type"]
    steps = _STEPS.get(ctype, _STEPS["общий"])

    picked = []
    try:
        picked = select_examples(load_examples(user_dir), ctype, max_examples)
    except Exception:
        picked = []

    lines = [
        f"=== СЦЕНАРИЙ: ОТВЕТ НА ОТЗЫВ — тип: {_TYPE_TITLES.get(ctype, ctype)} ===",
        "Это не вопрос клиента, а задача написать публичный ответ на уже опубликованный отзыв. "
        "Пиши ответ от лица компании, готовым текстом.",
        "Порядок ответа (все шаги обязательны):",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines.append("Чего в ответе быть не должно:")
    lines += [f"- {f}" for f in _COMMON_FORBIDDEN]

    if picked:
        lines.append("Примеры ответов компании на похожие отзывы "
                     "(держи стиль, тон и порядок мыслей; дословно не копируй):")
        for i, e in enumerate(picked, 1):
            if e["type"] == "положительный" and not e["complaint"]:
                lines.append(f"{i}. Ответ: «{clip(e['answer'], _MAX_ANSWER_CLIP)}»")
            else:
                lines.append(f"{i}. Отзыв: «{clip(e['complaint'], _MAX_COMPLAINT_CLIP)}»")
                lines.append(f"   Ответ: «{clip(e['answer'], _MAX_ANSWER_CLIP)}»")

    block = "\n".join(lines)
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[:_MAX_BLOCK_CHARS].rsplit("\n", 1)[0] + "\n…"
    sources = []
    for e in picked:
        if e["source"] and e["source"] not in sources:
            sources.append(e["source"])
    return block, sources
