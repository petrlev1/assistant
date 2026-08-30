# web_app.py - Веб-интерфейс для RAG-системы (с авторизацией)
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_file, abort, make_response
import threading
import uuid
import logging
import asyncio
import os
import io
import zipfile
import time
import json
import re
import secrets
import shutil
from rag_core import get_rag_system, get_user_rag, drop_user_rag, RAGSettings, DEFAULT_BASE_PROMPT, build_greeting, parse_price_list, detect_doc_group, _read_text_preview
from chat_logger import get_chat_logger
from auth_db import init_db, register_user, login_user, init_chat_history, save_message, get_history, add_document, delete_document, get_user_documents, clear_chat_history, delete_message, delete_message_pair, get_user_prompt, set_user_prompt, get_price_files, replace_price_items, delete_price_items_for_file, update_document_group, init_query_analytics, save_query_analytics, get_analytics, delete_user, delete_user_analytics, get_all_users_with_stats
import docs_renderer

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация логгера чата
chat_logger = get_chat_logger()

app = Flask(__name__)


def _load_secret_key():
    """Стабильный секретный ключ для сессий: env → файл → новый с сохранением в файл.

    Без этого ключ генерируется заново при каждом старте, и все пользователи
    разлогиниваются после рестарта сервера (нужно снова вводить пароль).
    """
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    try:
        with open(key_file, 'r', encoding='utf-8') as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = os.urandom(24).hex()
    try:
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(key)
    except OSError:
        pass
    return key


app.secret_key = _load_secret_key()

# === Базовые меры безопасности: rate-limit и CSRF ===

# Простейший in-memory rate-limit для /ask (дорогие вызовы LLM + эмбеддингов).
# Ключ — user_id, окно 60 с, лимит 30 запросов. Человеку хватает, скриптовый спам отсекается.
_rate_lock = threading.Lock()
_rate_hits = {}  # key -> list[float] (таймстемпы)


def _rate_limited(key, limit=30, window=60):
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return True
        hits.append(now)
        _rate_hits[key] = hits
        return False


def _origin_matches_host():
    """True, если Origin (если он есть) совпадает с Host — защита от CSRF.

    Браузер шлёт Origin на кросс-доменных POST (и на fetch). Если он есть и не
    совпадает с нашим хостом — это CSRF из чужого сайта. Отсутствие Origin
    (curl, сервер-к-серверу) пропускаем: CSRF грозит только браузерам.
    """
    origin = request.headers.get('Origin')
    if not origin:
        return True
    try:
        from urllib.parse import urlparse
        o_host = (urlparse(origin).hostname or '').lower()
    except Exception:
        return False
    host = request.host.split(':')[0].split('@')[-1].lower()
    return o_host == host


@app.before_request
def csrf_protect():
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        if not _origin_matches_host():
            logger.warning(f"🚫 CSRF отклонён: Origin={request.headers.get('Origin')} Host={request.host}")
            return jsonify({'error': 'Неверный источник запроса'}), 403

# RAG-система: модель эмбеддингов прогревается в фоне, а база знаний каждого
# пользователя хранится в ОТДЕЛЬНОМ экземпляре RAGCore (изоляция, без гонок
# данных — см. rag_core.get_user_rag). rag_ready = модель загружена, можно отвечать.
rag_ready = False

# Состояние Telegram-бота (общее с run_gui.py)
telegram_bot = None
telegram_thread = None
telegram_running = False


def initialize_rag_system():
    """Прогрев модели эмбеддингов в отдельном потоке (чтобы первый запрос не блокировался)."""
    global rag_ready
    try:
        logger.info("Запуск инициализации RAG-системы...")
        get_rag_system()  # грузит общий синглтон модели эмбеддингов (+ общую БЗ)
        rag_ready = True
        logger.info("RAG-система успешно инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации RAG-системы: {e}")


# === Фоновая переиндексация БЗ (не блокирует HTTP-ответ) ===

_reindex_lock = threading.Lock()
_reindex_needed = {}    # user_id -> True, если во время индексации появились новые файлы
_reindex_running = set()  # user_id, для которых уже крутится фоновый worker


def _reindex_user_async(user_id):
    """Переиндексировать БЗ пользователя в фоне.

    Загрузка/удаление большого прайса или CSV раньше блокировала HTTP-ответ на 1-2 мин
    (пересборка эмбеддингов + BM25). Теперь ответ уходит сразу, а переиндексация
    выполняется в отдельном потоке. Один worker на пользователя: если во время
    индексации подъехали новые файлы - worker делает ещё один проход.
    """
    with _reindex_lock:
        _reindex_needed[user_id] = True
        if user_id in _reindex_running:
            return  # worker уже работает и подхватит новый файл
        _reindex_running.add(user_id)

    def _worker():
        while True:
            try:
                get_user_rag(user_id).load_for_user(user_id)
            except Exception as e:
                logger.error(f"Ошибка фоновой переиндексации пользователя #{user_id}: {e}")
            with _reindex_lock:
                if _reindex_needed.get(user_id):
                    _reindex_needed[user_id] = False
                    continue  # ещё проход — за время индексации добавились файлы
                _reindex_running.discard(user_id)
                break

    threading.Thread(target=_worker, daemon=True).start()
    logger.info(f"🔄 Фоновая переиндексация БЗ пользователя #{user_id} запущена")


_group_backfill_lock = threading.Lock()
_group_backfill_running = set()


def _backfill_doc_groups_async(user_id, pending_docs):
    """Доопределяет doc_group старых документов в фоне (не блокирует /api/documents).

    Раньше это делалось синхронно прямо в ответе: для каждого файла без doc_group
    читалось превью (PDF/Excel) и определялась группа — при большом числе старых
    файлов панель открывалась с задержкой. Теперь ответ уходит сразу (фронт временно
    группирует по имени), а группы по содержимому дописываются в БД к следующему запросу.
    """
    if not pending_docs:
        return
    with _group_backfill_lock:
        if user_id in _group_backfill_running:
            return
        _group_backfill_running.add(user_id)

    def _worker():
        try:
            for d in pending_docs:
                file_path = os.path.join('Database', f'user_{user_id}', d['filename'])
                if not os.path.exists(file_path):
                    continue
                try:
                    preview = _read_text_preview(file_path)
                    group = detect_doc_group(d['filename'], preview, bool(d.get('is_price_list')))
                    update_document_group(d['id'], user_id, group)
                    logger.info(f"🗂️ Группа для {d['filename']}: {group}")
                except Exception as e:
                    logger.error(f"❌ Ошибка бэкфилла группы {d['filename']}: {e}")
        finally:
            with _group_backfill_lock:
                _group_backfill_running.discard(user_id)

    threading.Thread(target=_worker, daemon=True).start()


def init_auth():
    """Инициализация БД аутентификации и истории чата"""
    try:
        init_db()
        init_chat_history()
        init_query_analytics()
        logger.info("База данных аутентификации инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД аутентификации: {e}")


def _device_id():
    """Идентификатор устройства (браузера) из cookie device_id.

    Каждое устройство получает свой UUID при первом заходе — у каждого
    устройства свой чат, а аналитика остаётся общей на логин.
    """
    did = request.cookies.get('device_id')
    if not did or len(did) > 64:
        did = uuid.uuid4().hex
    return did


# === Маршруты аутентификации ===

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    # Уже вошли в систему — сразу в чат, без повторного ввода пароля
    if request.method == 'GET' and 'user_id' in session:
        return redirect(url_for('chat'))
    if request.method == 'GET':
        return render_template('login.html')

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    success, result = login_user(username, password)
    if success:
        session['user_id'] = result['id']
        session['username'] = result['username']
        logger.info(f"Пользователь {username} вошёл в систему")
        # Заранее создаём/загружаем базу знаний пользователя (модель уже прогрета)
        if rag_ready:
            get_user_rag(session['user_id'])
        return redirect(url_for('chat'))
    else:
        flash(result, 'error')
        return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Страница регистрации"""
    if request.method == 'GET':
        return render_template('register.html')

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm', '')

    if password != confirm:
        flash('Пароли не совпадают', 'error')
        return render_template('register.html')

    success, message = register_user(username, password)
    if success:
        flash('Регистрация успешна! Теперь вы можете войти.', 'success')
        return redirect(url_for('login'))
    else:
        flash(message, 'error')
        return render_template('register.html')


@app.route('/logout')
def logout():
    """Выход из системы"""
    username = session.get('username', 'Пользователь')
    session.clear()
    logger.info(f"Пользователь {username} вышел из системы")
    return redirect(url_for('login'))


# === Основные маршруты ===

@app.route('/')
def index():
    """Главная страница — презентация продукта (публичная)"""
    return render_template('about.html', logged_in='user_id' in session, username=session.get('username', ''))


@app.route('/chat')
def chat():
    """Страница чата (требуется авторизация)"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    # Создаём/подгружаем базу знаний текущего пользователя (отдельный изолированный RAGCore).
    # Модель эмбеддингов уже прогрета в фоне; первая загрузка строит эмбеддинги/BM25.
    if rag_ready:
        get_user_rag(session['user_id'])
    # Приветствие формируется из персонального промта: бот представляется своей ролью
    user_prompt = get_user_prompt(session['user_id'])
    greeting = build_greeting(user_prompt, session.get('username', 'Пользователь'))
    did = _device_id()
    resp = make_response(render_template('index.html', greeting=greeting))
    resp.set_cookie('device_id', did, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


@app.route('/about')
def about():
    """Публичная страница с описанием и возможностями системы (для клиентов)"""
    return render_template('about.html', logged_in='user_id' in session, username=session.get('username', ''))


# === Пользовательская документация (/docs) ===
# Порядок страниц = порядок в боковом меню раздела. Файлы лежат в docs/*.md.
DOCS_PAGES = [
    ('index', 'Обзор системы'),
    ('quickstart', 'Быстрый старт'),
    ('chat', 'Чат с ассистентом'),
    ('documents', 'Документы и база знаний'),
    ('prices', 'Загрузка прайс-листа'),
    ('prompt', 'Персональный промт'),
    ('analytics', 'Аналитика запросов'),
    ('faq', 'Частые вопросы'),
]
_DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')


@app.route('/docs')
@app.route('/docs/<page>')
def docs(page='index'):
    """Пользовательская документация (публичный раздел, без авторизации).

    Контент — markdown-файлы в docs/, рендерятся собственным мини-рендерером
    docs_renderer.py (без внешних зависимостей, одинаково на всех платформах).
    """
    if page not in dict(DOCS_PAGES):
        abort(404)
    doc_path = os.path.join(_DOCS_DIR, f'{page}.md')
    try:
        with open(doc_path, 'r', encoding='utf-8') as f:
            md_text = f.read()
    except OSError:
        abort(404)
    return render_template('docs.html',
                           pages=DOCS_PAGES,
                           current_page=page,
                           page_title=dict(DOCS_PAGES)[page],
                           content_html=docs_renderer.render(md_text),
                           logged_in='user_id' in session,
                           username=session.get('username', ''))


@app.route('/ask', methods=['POST'])
def ask_question():
    """Обработка вопроса от пользователя (требуется авторизация)"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    # Rate-limit: защита от спама дорогими LLM/эмбеддинг-вызовами (30 запросов/мин на пользователя)
    if _rate_limited(f"ask:{session['user_id']}"):
        return jsonify({'error': 'Слишком много запросов. Подождите немного.'}), 429

    try:
        data = request.get_json()
        question = data.get('question', '').strip()
        user_id = session['user_id']
        user_name = session.get('username', 'Пользователь')

        if not question:
            return jsonify({'error': 'Пустой вопрос'}), 400

        if not rag_ready:
            return jsonify({'answer': 'Система еще инициализируется. Пожалуйста, подождите...'}), 200

        # База знаний конкретного пользователя — отдельный изолированный экземпляр RAGCore
        user_rag = get_user_rag(user_id)

        logger.info(f"Вопрос от {user_name}: {question}")

        # Сохраняем вопрос в историю (свой чат на каждом устройстве)
        device_id = _device_id()
        user_msg_id = save_message(user_id, 'user', question, device_id=device_id)

        # Логирование вопроса в файл чата (с провайдером и моделью)
        provider = user_rag.settings.get("llm_provider", "")
        model = user_rag.settings.get("llm_model", "")
        chat_logger.log_message(user_name, user_id, question, is_bot=False, provider=provider, model=model)

        # Персональный промт пользователя (если задан — заменит системный промт по умолчанию)
        user_prompt = get_user_prompt(user_id)

        answer = user_rag.ask_model(question, user_prompt=user_prompt)
        logger.info("Ответ сгенерирован успешно")

        # Сохраняем ответ в историю
        assistant_msg_id = save_message(user_id, 'assistant', answer, device_id=device_id)

        # Логирование ответа
        chat_logger.log_message("Бот", user_id, answer, is_bot=True, provider=provider, model=model)

        # Сохраняем пару вопрос-ответ в аналитику (переживает очистку чата)
        save_query_analytics(user_id, question, answer)

        resp = jsonify({'answer': answer, 'user_msg_id': user_msg_id, 'assistant_msg_id': assistant_msg_id})
        resp.set_cookie('device_id', device_id, max_age=365 * 24 * 3600, samesite='Lax')
        return resp

    except Exception as e:
        logger.exception(f"Ошибка обработки вопроса: {e}")  # полный traceback в лог
        # Вопрос сохраняется в аналитику даже при ошибке (пустой ответ = кандидат в пробелы базы)
        try:
            if 'user_id' in locals() and 'question' in locals() and question:
                save_query_analytics(user_id, question, None)
        except Exception:
            pass
        return jsonify({'error': 'Внутренняя ошибка при обработке вопроса. Попробуйте ещё раз.'}), 500


@app.route('/history')
def get_chat_history():
    """Получение истории чата текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    did = _device_id()
    messages = get_history(session['user_id'], device_id=did)
    resp = jsonify({'messages': messages})
    resp.set_cookie('device_id', did, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


@app.route('/analytics')
def analytics():
    """Страница аналитики запросов — только данные текущего пользователя"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    stats = get_analytics(session['user_id'])
    return render_template('analytics.html', stats=stats)


@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    """Очистка истории чата текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    did = _device_id()
    success = clear_chat_history(session['user_id'], device_id=did)
    if success:
        logger.info(f"🗑️ Пользователь {session.get('username')} очистил историю чата (устройство {did[:8]}…)")
        resp = jsonify({'success': True, 'message': 'История чата очищена'})
        resp.set_cookie('device_id', did, max_age=365 * 24 * 3600, samesite='Lax')
        return resp
    else:
        return jsonify({'error': 'Ошибка при очистке истории'}), 500


@app.route('/api/chat/delete/<int:message_id>', methods=['POST'])
def delete_chat_message(message_id):
    """Удаление одного сообщения из истории чата"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    success = delete_message(message_id, session['user_id'])
    if success:
        logger.info(f"🗑️ Пользователь {session.get('username')} удалил сообщение #{message_id}")
        return jsonify({'success': True, 'message': 'Сообщение удалено'})
    else:
        return jsonify({'error': 'Сообщение не найдено'}), 404


@app.route('/api/chat/delete-pair/<int:message_id>', methods=['POST'])
def delete_chat_message_pair(message_id):
    """Удаление пары: ответ бота + предыдущий вопрос пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    success, deleted_ids = delete_message_pair(message_id, session['user_id'])
    if success:
        logger.info(f"🗑️ Пользователь {session.get('username')} удалил пару #{deleted_ids}")
        return jsonify({'success': True, 'deleted_ids': deleted_ids})
    else:
        return jsonify({'error': 'Сообщение не найдено'}), 404


# === Персональный промт пользователя ===

@app.route('/api/prompt', methods=['GET'])
def get_prompt():
    """Получение персонального промта текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    prompt = get_user_prompt(session['user_id'])
    return jsonify({
        'prompt': prompt or '',
        'has_custom': bool(prompt),
        'default_prompt': DEFAULT_BASE_PROMPT,
    })


@app.route('/api/prompt', methods=['POST'])
def save_prompt():
    """Сохранение персонального промта текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()

    if set_user_prompt(session['user_id'], prompt):
        logger.info(f"📝 Пользователь {session.get('username')} сохранил персональный промт ({len(prompt)} симв.)")
        return jsonify({'success': True, 'has_custom': bool(prompt)})
    return jsonify({'error': 'Не удалось сохранить промт'}), 500


@app.route('/status')
def status():
    """Проверка статуса системы"""
    if 'user_id' not in session:
        return jsonify({'initialized': False}), 401
    return jsonify({'initialized': rag_ready})


# === Управление документами пользователя ===

@app.route('/api/documents')
def get_documents():
    """Список документов текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']
    user_db_folder = os.path.join('Database', f'user_{user_id}')

    # Синхронизируем файлы на диске с записями в БД
    if os.path.exists(user_db_folder):
        files_on_disk = set(os.listdir(user_db_folder))
        db_docs = get_user_documents(user_id)
        db_filenames = {d['filename'] for d in db_docs}

        # Файлы, которые есть на диске, но нет в БД — добавляем
        for fname in files_on_disk:
            file_path = os.path.join(user_db_folder, fname)
            if os.path.isfile(file_path) and fname not in db_filenames:
                add_document(user_id, fname, fname)

        # Файлы, которые есть в БД, но нет на диске — удаляем записи
        for doc in db_docs:
            if doc['filename'] not in files_on_disk:
                delete_document(doc['id'], user_id)

    # Возвращаем актуальный список
    docs = get_user_documents(user_id)
    # Пометка прайс-листов: сколько позиций распарсено в price_items для каждого файла
    try:
        price_files = get_price_files(user_id)
        for d in docs:
            d['is_price_list'] = d['filename'] in price_files
            d['price_count'] = price_files.get(d['filename'], 0)
    except Exception as e:
        logger.error(f"Ошибка получения прайсов для списка документов: {e}")
    # Фоновая миграция групп старых документов: doc_group по содержимому дописывается
    # в БД в фоне (не блокирует ответ; фронт временно группирует по имени).
    _backfill_doc_groups_async(user_id, [d for d in docs if not d.get('doc_group')])
    # Пометка TXT-файлов, созданных для эмбеддингов (есть одноимённый файл другого формата)
    try:
        stems = {}
        for d in docs:
            stem = os.path.splitext(d['original_name'])[0].lower()
            stems.setdefault(stem, []).append(d)
        for d in docs:
            d['is_rag_helper'] = False
            if d['original_name'].lower().endswith('.txt'):
                stem = os.path.splitext(d['original_name'])[0].lower()
                has_twin = any(x['id'] != d['id'] and not x['original_name'].lower().endswith('.txt')
                               for x in stems.get(stem, []))
                if has_twin:
                    d['is_rag_helper'] = True
                    logger.info(f"🙈 RAG-файл (скрывается по умолчанию): {d['original_name']}")
    except Exception as e:
        logger.error(f"Ошибка пометки RAG-файлов: {e}")
    # TXT-дубликаты наследуют группу основного файла (одноимённый не-txt), чтобы быть в одной группе
    try:
        twin_stems = {}
        for d in docs:
            stem = os.path.splitext(d['original_name'])[0].lower()
            twin_stems.setdefault(stem, []).append(d)
        for d in docs:
            if d.get('is_rag_helper'):
                stem = os.path.splitext(d['original_name'])[0].lower()
                twin = next((x for x in twin_stems.get(stem, [])
                             if x['id'] != d['id'] and not x['original_name'].lower().endswith('.txt')), None)
                if twin and twin.get('doc_group') and d.get('doc_group') != twin['doc_group']:
                    update_document_group(d['id'], user_id, twin['doc_group'])
                    d['doc_group'] = twin['doc_group']
                    logger.info(f"🔗 RAG-файл {d['original_name']} наследует группу основного файла: {twin['doc_group']}")
    except Exception as e:
        logger.error(f"Ошибка наследования группы RAG-файлов: {e}")
    return jsonify({'documents': docs})


@app.route('/api/documents/upload', methods=['POST'])
def upload_document():
    """Загрузка одного или нескольких документов в базу знаний пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    # Поддержка нескольких файлов: поле 'files' (список) или одно поле 'file'
    files = request.files.getlist('files')
    if not files and 'file' in request.files:
        files = [request.files['file']]
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({'error': 'Файл не выбран'}), 400

    # Проверка расширений
    allowed_ext = ('.txt', '.pdf', '.docx', '.csv', '.xlsx')
    bad_files = [f.filename for f in files if not f.filename.lower().endswith(allowed_ext)]
    if bad_files:
        return jsonify({'error': f'Неподдерживаемый формат: {", ".join(bad_files)}. Разрешены: {", ".join(allowed_ext)}'}), 400

    user_id = session['user_id']
    user_db_folder = os.path.join('Database', f'user_{user_id}')
    os.makedirs(user_db_folder, exist_ok=True)

    # Ручной флажок «Это прайс-лист» (если автодетект не сработал)
    is_price_flag = request.form.get('is_price_list') in ('1', 'true', 'on')

    results = []
    total_price = 0
    any_price = False

    # Существующие документы пользователя — для обновления при повторной загрузке
    existing_docs = get_user_documents(user_id)
    existing_by_name = {d['filename']: d for d in existing_docs}

    for file in files:
        # Защита от path traversal: оставляем только имя файла без пути
        # (os.path.basename отсекает путь на обеих платформах, кириллицу не трогает)
        filename = os.path.basename(file.filename)
        if not filename:
            results.append({'filename': file.filename, 'success': False, 'error': 'Некорректное имя файла'})
            continue
        file_path = os.path.join(user_db_folder, filename)
        file.save(file_path)

        # Повторная загрузка файла с тем же именем = обновление документа, а не дубликат
        old_doc = existing_by_name.get(filename)
        if old_doc is not None:
            delete_document(old_doc['id'], user_id)
            delete_price_items_for_file(user_id, filename)
            existing_by_name.pop(filename, None)
            logger.info(f"♻️ Обновление документа: {filename} (заменяет id {old_doc['id']})")

        # Определяем группу документа (имя файла + превью содержимого)
        preview = _read_text_preview(file_path)
        doc_group = detect_doc_group(filename, preview)
        # Добавляем запись в БД
        success, doc_id = add_document(user_id, filename, filename, doc_group=doc_group)
        if not success:
            os.remove(file_path)
            results.append({'filename': filename, 'success': False, 'error': 'Ошибка при сохранении в БД'})
            continue

        # Прайс-лист: автоопределение (xlsx/csv) или ручной флажок.
        # Парсер сам решает, похож ли файл на прайс (шапка с ценой+наименованием и есть строки с ценой).
        price_parsed = 0
        is_price_list = False
        if filename.lower().endswith(('.xlsx', '.csv')):
            try:
                price_rows = parse_price_list(file_path)
            except Exception as e:
                logger.error(f"❌ Ошибка разбора прайс-листа {filename}: {e}")
                price_rows = []
            if price_rows:
                is_price_list = True
                price_parsed = len(price_rows)
                replace_price_items(user_id, filename, price_rows)
                logger.info(f"📋 Прайс-лист {filename}: {price_parsed} позиций (user {user_id})")
                update_document_group(doc_id, user_id, '💲 Прайс-листы')
            elif is_price_flag:
                logger.warning(f"⚠️ {filename} помечен как прайс-лист, но парсер не нашёл таблицу с ценой и наименованием")

        total_price += price_parsed
        any_price = any_price or is_price_list
        results.append({'filename': filename, 'success': True, 'is_price_list': is_price_list, 'price_count': price_parsed})
        logger.info(f"📄 Пользователь {session.get('username')} загрузил документ: {filename}")

    # Переиндексация БЗ в фоне — большой прайс/CSV не блокирует HTTP-ответ
    if rag_ready:
        _reindex_user_async(user_id)

    uploaded = sum(1 for r in results if r['success'])
    failed = [r for r in results if not r['success']]
    if uploaded == 0:
        return jsonify({'success': False, 'error': 'Ни один файл не загружен'}), 500

    message = f'Загружено документов: {uploaded}'
    if failed:
        message += f', ошибок: {len(failed)}'
    if any_price:
        message += f'. Прайс-листы: {total_price} позиций'
    return jsonify({'success': True, 'message': message, 'results': results,
                    'is_price_list': any_price, 'price_count': total_price})


@app.route('/api/documents/delete/<int:doc_id>', methods=['POST'])
def delete_document_route(doc_id):
    """Удаление документа пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']

    # Получаем информацию о документе
    docs = get_user_documents(user_id)
    doc_info = next((d for d in docs if d['id'] == doc_id), None)

    if not doc_info:
        return jsonify({'error': 'Документ не найден'}), 404

    # Удаляем файл
    file_path = os.path.join('Database', f'user_{user_id}', doc_info['filename'])
    if os.path.exists(file_path):
        os.remove(file_path)
        logger.info(f"🗑️ Удалён файл: {file_path}")

    # Удаляем запись из БД
    delete_document(doc_id, user_id)
    # Если файл был прайс-листом — удаляем его позиции из price_items
    delete_price_items_for_file(user_id, doc_info['filename'])

    # Переиндексация БЗ в фоне
    if rag_ready:
        _reindex_user_async(user_id)

    logger.info(f"🗑️ Пользователь {session.get('username')} удалил документ: {doc_info['filename']}")
    return jsonify({'success': True, 'message': 'Документ удалён'})


@app.route('/api/documents/download/<int:doc_id>')
def download_document(doc_id):
    """Скачивание документа пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']

    # Получаем информацию о документе
    docs = get_user_documents(user_id)
    doc_info = next((d for d in docs if d['id'] == doc_id), None)

    if not doc_info:
        return jsonify({'error': 'Документ не найден'}), 404

    file_path = os.path.join('Database', f'user_{user_id}', doc_info['filename'])
    if not os.path.exists(file_path):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    logger.info(f"⬇️ Пользователь {session.get('username')} скачал документ: {doc_info['filename']}")
    return send_file(
        file_path,
        as_attachment=True,
        download_name=doc_info['original_name']
    )

@app.route('/api/documents/delete-bulk', methods=['POST'])
def delete_documents_bulk():
    """Удаление нескольких документов пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return jsonify({'error': 'Документы не выбраны'}), 400

    user_id = session['user_id']
    docs = get_user_documents(user_id)
    by_id = {d['id']: d for d in docs}

    deleted_names = []
    for doc_id in ids:
        doc_info = by_id.get(doc_id)
        if not doc_info:
            continue
        file_path = os.path.join('Database', f'user_{user_id}', doc_info['filename'])
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"🗑️ Удалён файл: {file_path}")
        delete_document(doc_id, user_id)
        delete_price_items_for_file(user_id, doc_info['filename'])
        deleted_names.append(doc_info['filename'])

    if not deleted_names:
        return jsonify({'error': 'Документы не найдены'}), 404

    # Переиндексация БЗ в фоне
    if rag_ready:
        _reindex_user_async(user_id)

    logger.info(f"🗑️ Пользователь {session.get('username')} удалил документы: {', '.join(deleted_names)}")
    return jsonify({'success': True, 'message': f'Удалено документов: {len(deleted_names)}', 'deleted': deleted_names})


@app.route('/api/documents/download-bulk')
def download_documents_bulk():
    """Скачивание нескольких документов одним ZIP-архивом"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    ids = [int(x) for x in request.args.get('ids', '').split(',') if x.strip().isdigit()]
    if not ids:
        return jsonify({'error': 'Документы не выбраны'}), 400

    user_id = session['user_id']
    docs = get_user_documents(user_id)
    by_id = {d['id']: d for d in docs}

    buf = io.BytesIO()
    used_names = set()
    added = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for doc_id in ids:
            doc_info = by_id.get(doc_id)
            if not doc_info:
                continue
            file_path = os.path.join('Database', f'user_{user_id}', doc_info['filename'])
            if not os.path.exists(file_path):
                continue
            # Уникализируем имя внутри архива (два файла с одинаковым именем)
            name = doc_info['original_name']
            base, ext = os.path.splitext(name)
            n = 2
            arcname = name
            while arcname in used_names:
                arcname = f'{base} ({n}){ext}'
                n += 1
            used_names.add(arcname)
            zf.write(file_path, arcname=arcname)
            added += 1
            logger.info(f"⬇️ В архив: {doc_info['filename']}")

    if added == 0:
        return jsonify({'error': 'Файлы не найдены на диске'}), 404

    buf.seek(0)
    logger.info(f"⬇️ Пользователь {session.get('username')} скачал архив из {added} документов")
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name='documents.zip')



@app.route('/api/kb/download/<path:filename>')
def download_kb_file(filename):
    """Скачивание файла из базы знаний по имени (клик по источнику в чате).
    Ищем сначала в папке пользователя, затем в общей базе.
    Если запрошен .txt, а рядом лежит одноимённый файл другого формата
    (например myfile.pdf) — для скачивания отдаём оригинал (приоритет .pdf)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    safe_name = os.path.basename(filename)  # защита от path traversal
    if not safe_name:
        return jsonify({'error': 'Неверное имя файла'}), 400

    user_id = session['user_id']
    candidates = [
        os.path.join('Database', f'user_{user_id}'),
        os.path.join('Database'),
    ]

    def _pick_download(folder):
        """Выбирает файл для скачивания из папки: (путь, имя_для_скачивания) или None."""
        if not os.path.isdir(folder):
            return None
        # Если запрошен .txt и в папке есть одноимённый файл другого формата —
        # отдаём оригинал (приоритет .pdf)
        if safe_name.lower().endswith('.txt'):
            stem = os.path.splitext(safe_name)[0].lower()
            siblings = [
                f for f in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, f))
                and os.path.splitext(f)[0].lower() == stem
                and f.lower() != safe_name.lower()
            ]
            if siblings:
                siblings.sort(key=lambda f: (not f.lower().endswith('.pdf'), f.lower()))
                original = siblings[0]
                logger.info(f"📎 Для источника {safe_name} скачивается оригинал: {original}")
                return os.path.join(folder, original), original
        direct = os.path.join(folder, safe_name)
        if os.path.isfile(direct):
            return direct, safe_name
        return None

    for folder in candidates:
        hit = _pick_download(folder)
        if hit:
            file_path, download_name = hit
            logger.info(f"⬇️ Пользователь {session.get('username')} скачал из чата: {download_name}")
            return send_file(file_path, as_attachment=True, download_name=download_name)

    return jsonify({'error': 'Файл не найден'}), 404


# === Telegram-бот ===

def _run_telegram_bot():
    """Запуск Telegram-бота в отдельном потоке"""
    global telegram_bot, telegram_running
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        telegram_bot.run()
    except Exception as e:
        logger.error(f"Ошибка в работе Telegram бота: {e}", exc_info=True)
    finally:
        telegram_running = False


@app.route('/telegram/status')
def telegram_status():
    """Статус Telegram-бота"""
    if 'user_id' not in session:
        return jsonify({'running': False, 'has_token': False}), 401
    global telegram_running
    settings = RAGSettings()
    token = (settings.get("telegram_bot_token") or "").strip()
    has_token = bool(token and token != "YOUR_BOT_TOKEN_HERE" and len(token) > 20)
    return jsonify({
        'running': telegram_running,
        'has_token': has_token,
    })


@app.route('/telegram/start', methods=['POST'])
def telegram_start():
    """Запуск Telegram-бота"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Необходима авторизация'}), 401

    global telegram_bot, telegram_thread, telegram_running

    if telegram_running:
        return jsonify({'success': True, 'message': 'Бот уже запущен'})

    data = request.get_json(silent=True) or {}
    token = (data.get('token') or "").strip()
    if not token:
        settings = RAGSettings()
        token = (settings.get("telegram_bot_token") or "").strip()

    if not token or token == "YOUR_BOT_TOKEN_HERE" or len(token) < 20:
        return jsonify({'success': False, 'error': 'Введите валидный Telegram Bot Token'}), 400

    try:
        settings = RAGSettings()
        settings.set("telegram_bot_token", token)
        settings.save_settings()

        from telegram_bot import TelegramRAGBot
        telegram_bot = TelegramRAGBot(token)
        telegram_running = True
        telegram_thread = threading.Thread(target=_run_telegram_bot, daemon=True)
        telegram_thread.start()

        return jsonify({'success': True, 'message': 'Бот запущен. Проверьте Telegram.'})
    except Exception as e:
        telegram_running = False
        logger.exception(f"Ошибка запуска Telegram бота: {e}")
        return jsonify({'success': False, 'error': 'Не удалось запустить бота'}), 500


@app.route('/telegram/stop', methods=['POST'])
def telegram_stop():
    """Остановка Telegram-бота"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Необходима авторизация'}), 401

    global telegram_bot, telegram_running

    if not telegram_running:
        return jsonify({'success': True, 'message': 'Бот не был запущен'})

    try:
        if telegram_bot:
            telegram_bot.stop()
        telegram_running = False
        return jsonify({'success': True, 'message': 'Бот остановлен'})
    except Exception as e:
        logger.exception(f"Ошибка остановки Telegram бота: {e}")
        return jsonify({'success': False, 'error': 'Не удалось остановить бота'}), 500



# === Админ-панель (скрытая страница /admin — ссылок на неё нигде нет) ===
# Учётка админа НЕ в git: admin_config.json (gitignored, как db_config.json) либо
# переменные окружения ADMIN_USERNAME / ADMIN_PASSWORD. Если ничего не задано —
# вход в админку отключён (все попытки логина отклоняются).
_ADMIN_CONFIG_FILE = "admin_config.json"


def _load_admin_credentials():
    """(username, password) админа: env → admin_config.json → (None, None)."""
    env_user = os.environ.get("ADMIN_USERNAME", "").strip()
    env_pass = os.environ.get("ADMIN_PASSWORD", "")
    if env_user and env_pass:
        return env_user, env_pass
    try:
        with open(_ADMIN_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        user = (cfg.get("admin_username") or "").strip()
        password = cfg.get("admin_password") or ""
        if user and password:
            return user, password
    except OSError:
        pass
    except ValueError:
        logger.error(f"⚠️ {_ADMIN_CONFIG_FILE} повреждён — вход в админ-панель отключён")
    return None, None


def _scrub_chat_logs(user_id):
    """Вычитка строк пользователя из файловых логов чата (chat_logs/*.log).

    Логи пишутся построчно с пометкой «(ID: N):». Перезапись через временный
    файл + os.replace; при ошибке (файл занят) файл пропускается — best-effort.
    """
    pattern = re.compile(rf"\(ID: {user_id}\):")
    removed = 0
    log_dir = "chat_logs"
    if not os.path.isdir(log_dir):
        return 0
    for fname in sorted(os.listdir(log_dir)):
        if not fname.startswith("chat_") or not fname.endswith(".log"):
            continue
        path = os.path.join(log_dir, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        kept = [ln for ln in lines if not pattern.search(ln)]
        if len(kept) == len(lines):
            continue
        removed += len(lines) - len(kept)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"⚠️ Не удалось вычистить {fname}: {e} (файл занят?)")
            try:
                os.remove(tmp)
            except OSError:
                pass
    if removed:
        logger.info(f"🧹 Из chat_logs вычищено {removed} строк пользователя #{user_id}")
    return removed


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    """Скрытая админ-панель: вход по отдельной учётке + список пользователей.

    Доступ только по прямому URL — ссылок на страницу нигде нет.
    """
    if request.method == 'POST':
        # Rate-limit на попытки входа (10 за 5 минут)
        if _rate_limited("admin_login", limit=10, window=300):
            flash("Слишком много попыток входа. Подождите немного.", 'error')
            return render_template('admin.html', mode='login'), 429
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        admin_user, admin_pass = _load_admin_credentials()
        if (admin_user and admin_pass and username == admin_user
                and secrets.compare_digest(password, admin_pass)):
            session['admin'] = True
            logger.info(f"🔐 Вход в админ-панель: {username}")
            return redirect(url_for('admin'))
        logger.warning(f"🚫 Неудачная попытка входа в админ-панель: {username}")
        flash("Неверное имя пользователя или пароль", 'error')

    if session.get('admin'):
        users = get_all_users_with_stats()
        # Название папки БЗ пользователя на диске (справка для админа; локально)
        for u in users:
            u['kb_folder'] = os.path.join('Database', f"user_{u['id']}")
            u['kb_folder_exists'] = os.path.isdir(u['kb_folder'])
        return render_template('admin.html', mode='dashboard', users=users)
    return render_template('admin.html', mode='login')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    """Выход из админ-панели (пользовательская сессия не трогается)."""
    session.pop('admin', None)
    return redirect(url_for('admin'))


@app.route('/admin/api/delete-user', methods=['POST'])
def admin_delete_user():
    """Полное удаление пользователя и всех его данных (только для админа)."""
    if not session.get('admin'):
        return jsonify({'error': 'Доступ запрещён'}), 403
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    if (not isinstance(user_id, int) or isinstance(user_id, bool)
            or user_id <= 0 or data.get('confirm') is not True):
        return jsonify({'error': 'Некорректный запрос'}), 400

    users = get_all_users_with_stats()
    user = next((u for u in users if u['id'] == user_id), None)
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    # 1) Выгружаем RAGCore из памяти (эмбеддинги/BM25 — гигабайты RAM)
    drop_user_rag(user_id)
    # 2) БД: каскад ON DELETE CASCADE чистит документы, прайсы, чат, аналитику
    ok, _ = delete_user(user_id)
    if not ok:
        return jsonify({'error': 'Ошибка удаления пользователя из БД'}), 500
    # 3) Файлы пользователя и кэш эмбеддингов на диске
    removed_dirs = []
    for folder in (os.path.join('Database', f'user_{user_id}'),
                   os.path.join('embeddings_cache', f'user_{user_id}')):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            removed_dirs.append(folder)
    # 4) Вычитка строк пользователя из файловых логов чата
    scrubbed = _scrub_chat_logs(user_id)
    logger.info(f"🗑️ Админ удалил пользователя {user['username']} (#{user_id}): "
                f"папки {removed_dirs}, строк логов: {scrubbed}")
    return jsonify({'success': True,
                    'message': f"Пользователь {user['username']} удалён со всеми данными",
                    'removed_dirs': removed_dirs, 'log_lines_scrubbed': scrubbed})


@app.route('/admin/api/delete-analytics', methods=['POST'])
def admin_delete_analytics():
    """Удаление аналитики запросов пользователя (сам пользователь сохраняется)."""
    if not session.get('admin'):
        return jsonify({'error': 'Доступ запрещён'}), 403
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return jsonify({'error': 'Некорректный запрос'}), 400
    deleted = delete_user_analytics(user_id)
    return jsonify({'success': True, 'deleted': deleted,
                    'message': f'Удалено записей аналитики: {deleted}'})


def create_app():
    """Создание Flask приложения"""
    # Инициализируем БД аутентификации
    init_auth()

    # Запускаем инициализацию RAG-системы в отдельном потоке
    init_thread = threading.Thread(target=initialize_rag_system, daemon=True)
    init_thread.start()

    return app
