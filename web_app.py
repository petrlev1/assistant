# web_app.py - Веб-интерфейс для RAG-системы (с авторизацией)
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_file, send_from_directory, abort, make_response
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
import csv
from datetime import datetime
from rag_core import get_rag_system, get_user_rag, drop_user_rag, RAGSettings, DEFAULT_BASE_PROMPT, build_greeting, parse_price_list, detect_doc_group, _read_text_preview, QA_CORRECTION_FILE, parse_qa_pairs_file, _iter_csv_rows
from chat_logger import get_chat_logger
from auth_db import init_db, register_user, login_user, init_chat_history, save_message, get_history, add_document, get_prompt_context, get_session_start, start_new_chat_session, get_all_settings, set_settings, delete_document, get_user_documents, clear_chat_history, delete_message, delete_message_pair, get_user_prompt, set_user_prompt, get_price_files, replace_price_items, delete_price_items_for_file, update_document_group, init_query_analytics, save_query_analytics, get_analytics, delete_user, delete_user_analytics, get_all_users_with_stats
from auth_db import (init_widgets, create_widget, list_user_widgets, update_widget,
                     delete_widget, widget_consume, get_widget_by_key, get_widget_history,
                     clear_widget_history)
from auth_db import (init_max_channels, get_max_channel, get_max_channel_by_hook,
                     list_active_max_channels, save_max_channel, update_max_channel,
                     delete_max_channel, max_consume)
import docs_renderer
import max_bot
import site_crawler
import model_catalog

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
        init_widgets()
        init_max_channels()
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


def _with_ts(messages):
    """Добавляет сообщениям epoch-метку 'ts' (клиенту нужен разделитель «новый диалог»).

    Дата берётся в серверном времени — так же, как chat_sessions.started_at, поэтому
    сравнение на клиенте не зависит от таймзоны браузера.
    """
    out = []
    for m in messages or []:
        m = dict(m)
        created = m.get('created_at')
        m['ts'] = created.timestamp() if hasattr(created, 'timestamp') else 0
        out.append(m)
    return out


def _chat_context(user_id, device_id, external=False):
    """История диалога для модели (память диалога, вариант B).

    Пусто, если память выключена настройками. external=True — виджет на сайте и MAX:
    там своя область device_id (без наследственной истории владельца 'web') и отдельный
    выключатель: у внешних собеседников — чужие люди, а не владелец базы.
    """
    key = "chat_memory_external" if external else "chat_memory_enabled"
    try:
        st = get_all_settings() or {}
    except Exception as e:
        logger.error(f"Не удалось прочитать настройки памяти диалога: {e}")
        return []
    if not st.get(key, True):
        return []
    try:
        return get_prompt_context(
            user_id, device_id,
            limit=int(st.get("chat_memory_max_messages", 20) or 20),
            max_chars=int(st.get("chat_memory_max_chars", 4000) or 4000),
            ttl_minutes=int(st.get("chat_memory_ttl_minutes", 120) or 0),
        )
    except (TypeError, ValueError) as e:
        logger.error(f"Некорректные настройки памяти диалога: {e}")
        return get_prompt_context(user_id, device_id)


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


# === Иконка сайта (favicon) ===
ICON_DIR = os.path.join(app.static_folder, 'img')


@app.route('/favicon.ico')
def favicon():
    """Классический favicon: браузеры и краулеры запрашивают /favicon.ico даже при наличии <link rel="icon">."""
    return send_from_directory(ICON_DIR, 'favicon.ico', mimetype='image/vnd.microsoft.icon')


@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    """Иконка для «на главный экран» в Safari/iOS."""
    return send_from_directory(ICON_DIR, 'apple-touch-icon.png', mimetype='image/png')


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
    ('site', 'База знаний по ссылке на сайт'),
    ('prices', 'Загрузка прайс-листа'),
    ('prompt', 'Персональный промт'),
    ('analytics', 'Аналитика запросов'),
    ('widget', 'Виджет на сайт'),
    ('max', 'Бот в мессенджере MAX'),
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

        # Память диалога: последние сообщения ЭТОГО устройства (см. chat_memory_* в настройках)
        history = _chat_context(user_id, device_id)

        answer = user_rag.ask_model(question, user_prompt=user_prompt, history=history)
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
    started = get_session_start(session['user_id'], did)
    resp = jsonify({'messages': _with_ts(messages),
                    'session_started_at': started.timestamp() if started else 0})
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


@app.route('/api/chat/new', methods=['POST'])
def new_chat_dialogue():
    """«Новый диалог»: модель перестаёт видеть прошлые сообщения (они остаются в истории).

    Ничего не удаляется — сдвигается граница, от которой читается контекст диалога.
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    did = _device_id()
    if not start_new_chat_session(session['user_id'], did):
        return jsonify({'error': 'Не удалось начать новый диалог'}), 500
    logger.info(f"🆕 Пользователь {session.get('username')} начал новый диалог (устройство {did[:8]}…)")
    resp = jsonify({'success': True})
    resp.set_cookie('device_id', did, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


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
    # Пометка: файл можно править текстом прямо из списка (.txt/.csv)
    for d in docs:
        d['can_edit'] = _is_editable(d['filename'])
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


# === Импорт сайта в базу знаний (обход по ссылке → TXT + авто-эмбеддинги) ===


def _site_pages_limit():
    """Лимит страниц обхода: app_settings.site_pages_limit, иначе значение по умолчанию."""
    try:
        value = int((get_all_settings() or {}).get('site_pages_limit', 0) or 0)
        if value > 0:
            return value
    except Exception as e:
        logger.warning(f"Не удалось прочитать лимит страниц сайта: {e}")
    return site_crawler.DEFAULT_PAGE_LIMIT


def _site_crawl_finished(user_id, result):
    """Итог обхода: файл сайта становится документом БЗ + фоновая переиндексация.

    Вызывается из потока обхода (после записи TXT). Прежний документ с тем же
    именем заменяется — как повторная загрузка файла, без дубликатов в списке.
    """
    filename = result['filename']
    os.makedirs(os.path.join('Database', f'user_{user_id}'), exist_ok=True)

    old = next((d for d in get_user_documents(user_id) if d['filename'] == filename), None)
    if old is not None:
        delete_document(old['id'], user_id)
        logger.info(f"♻️ Сайт: документ {filename} обновляется (заменяет id {old['id']})")

    success, doc_id = add_document(user_id, filename, filename,
                                   doc_group=f"🌐 {result['domain']}")
    if not success:
        raise RuntimeError('не удалось добавить документ в базу знаний')

    stats = result.get('stats') or {}
    logger.info(f"🌐 Сайт {result['domain']}: строк {result['lines']}, страниц {stats.get('pages', 0)} "
                f"(новых {stats.get('new', 0)}, изменённых {stats.get('changed', 0)}, "
                f"неизменных {stats.get('unchanged', 0)}, ушло {stats.get('gone', 0)}) "
                f"→ {filename} (doc {doc_id})")
    if rag_ready:
        _reindex_user_async(user_id)


@app.route('/api/site/crawl', methods=['POST'])
def site_crawl_start():
    """Запуск обхода сайта: ответ сразу, прогресс — GET /api/site/status."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Укажите адрес сайта'}), 400
    if '://' not in url:
        url = 'https://' + url
    try:
        pages = int(data.get('pages') or 0) or _site_pages_limit()
    except (TypeError, ValueError):
        pages = _site_pages_limit()
    respect_robots = bool(data.get('respect_robots', True))

    try:
        job = site_crawler.start_job(session['user_id'], url, pages, respect_robots,
                                     on_finish=_site_crawl_finished)
    except site_crawler.CrawlError as e:
        return jsonify({'error': str(e)}), 400

    logger.info(f"🌐 Пользователь {session.get('username')} запустил обход сайта: {url} (лимит {pages})")
    return jsonify({'success': True, 'job': site_crawler.public_job(job)})


@app.route('/api/site/status')
def site_crawl_status():
    """Состояние обхода для строки прогресса в интерфейсе."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    state = site_crawler.status_json()
    job = state.get('job') or {}
    if job.get('user_id') not in (None, session['user_id']):
        # Обход идёт у другого пользователя: свою строку прогресса ему показывать нечего
        return jsonify({'active': state['active'], 'job': None, 'other_user': True})
    return jsonify(state)


@app.route('/api/site/cancel', methods=['POST'])
def site_crawl_cancel():
    """Отмена обхода: в файл попадает то, что успели обойти."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    state = site_crawler.get_status()
    job = state.get('job') or {}
    if not state['active'] or job.get('user_id') != session['user_id']:
        return jsonify({'ok': False, 'error': 'Активного обхода нет'}), 409
    site_crawler.cancel()
    logger.info(f"⏹ Пользователь {session.get('username')} отменил обход сайта")
    return jsonify({'ok': True})


@app.route('/api/site/info')
def site_info():
    """Сведения о последнем обходе сайта для документа (кнопка «Обновить с сайта»)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    filename = os.path.basename(request.args.get('filename') or '')
    info = site_crawler.info_for_filename(session['user_id'], filename) if filename else None
    if not info:
        return jsonify({'error': 'Сайт для этого файла не найден'}), 404
    return jsonify(info)


@app.route('/api/kb/qa-correction', methods=['POST'])
def save_qa_correction():
    """Сохранение исправленного ответа в базу знаний (newdatabase.csv).

    Тело: {question, answer}. Тот же вопрос уже исправляли → обновляем ответ,
    иначе добавляем новую пару. После записи — фоновая переиндексация БЗ.
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    data = request.get_json() or {}
    question = (data.get('question') or '').strip()
    answer = (data.get('answer') or '').strip()
    if not question or not answer:
        return jsonify({'error': 'Вопрос и ответ не могут быть пустыми'}), 400

    user_id = session['user_id']
    user_folder = os.path.join('Database', f'user_{user_id}')
    os.makedirs(user_folder, exist_ok=True)
    file_path = os.path.join(user_folder, QA_CORRECTION_FILE)

    pairs = parse_qa_pairs_file(file_path)

    def _norm(s):
        return ' '.join(s.lower().split())

    norm_q = _norm(question)
    replaced = False
    for p in pairs:
        if _norm(p[0]) == norm_q:
            p[1] = answer
            replaced = True
            break
    if not replaced:
        pairs.append([question, answer])

    # Атомарная запись: временный файл + переименование (без половинок файла).
    # utf-8-sig (BOM) — чтобы Excel открывал файл без кракозябр.
    tmp_path = file_path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=',', quoting=csv.QUOTE_MINIMAL)
            writer.writerow(['Вопрос', 'Ответ'])
            for q, a in pairs:
                writer.writerow([q, a])
        os.replace(tmp_path, file_path)
    except Exception as e:
        logger.error(f"❌ Ошибка записи {file_path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({'error': 'Не удалось сохранить исправление'}), 500

    # Запись о документе — для списка «Документы» и ссылки-источника в чате
    existing = get_user_documents(user_id)
    if not any(d['filename'] == QA_CORRECTION_FILE for d in existing):
        add_document(user_id, QA_CORRECTION_FILE, QA_CORRECTION_FILE, doc_group='✅ Исправления')

    if rag_ready:
        _reindex_user_async(user_id)

    action = 'обновлено' if replaced else 'добавлено'
    logger.info(f"✏️ Пользователь {session.get('username')} {action} исправление: {question[:80]}")
    return jsonify({'success': True, 'message': f'Исправление {action} в базе знаний'})


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


# === Редактирование текстовых документов (TXT/CSV) прямо из списка документов ===
# Правка идёт в тот же файл в Database/user_N/, поэтому id документа не меняется
# (в отличие от повторной загрузки, где старый id удаляется и создаётся новый) —
# ссылки-источники в чате продолжают указывать на тот же документ.

EDITABLE_EXT = ('.txt', '.csv')
MAX_EDIT_BYTES = 1024 * 1024   # 1 МБ: больше textarea в браузере уже не тянет
EDIT_VERSIONS_KEEP = 5         # сколько последних версий файла храним


def _docs_dir(user_id):
    return os.path.join('Database', f'user_{user_id}')


def _versions_dir(user_id):
    """Папка версий файлов — ВНЕ папки пользователя.

    Иначе синхронизация в /api/documents и индексатор (rag_core перебирает файлы
    папки пользователя) могли бы принять версии за отдельные документы.
    """
    return os.path.join('Database', '.versions', f'user_{user_id}')


def _is_editable(filename):
    return filename.lower().endswith(EDITABLE_EXT)


def _get_owned_doc(user_id, doc_id):
    """Документ пользователя по id (None — если нет или он чужой)."""
    for d in get_user_documents(user_id):
        if d['id'] == doc_id:
            return d
    return None


def _detect_encoding(raw):
    """Кодировка текстового файла: utf-8-sig (BOM) → utf-8 → cp1251.

    Тот же порядок, что в rag_core._read_text_preview: файл, который индекс
    читает как UTF-8, редактор не должен перекодировать (иначе кириллица поедет).
    """
    if raw.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    for enc in ('utf-8', 'cp1251'):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return 'cp1251'   # не текстовый/смешанный — отдадим с заменами


def _read_text_file(path):
    """Текст файла + параметры для обратной записи (кодировка, переводы строк)."""
    with open(path, 'rb') as f:
        raw = f.read()
    encoding = _detect_encoding(raw)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode(encoding, errors='replace')
    crlf = raw.count(b'\r\n')
    lf = raw.count(b'\n') - crlf
    newline = '\r\n' if crlf > lf else '\n'
    return text, encoding, newline


def _restore_newlines(text, newline):
    """textarea в браузере всегда отдаёт \n — возвращаем родные переводы строк."""
    unified = text.replace('\r\n', '\n').replace('\r', '\n')
    if newline == '\r\n':
        return unified.replace('\n', '\r\n')
    return unified


def _write_text_file(path, text, encoding):
    """Атомарная запись (tmp + os.replace): обрыв не оставит половину файла."""
    tmp_path = path + '.tmp-edit'
    with open(tmp_path, 'wb') as f:
        f.write(text.encode(encoding))
    os.replace(tmp_path, path)


def _doc_etag(path):
    """Метка версии файла — защита от правки в двух вкладках одновременно."""
    st = os.stat(path)
    return f"{st.st_mtime_ns}-{st.st_size}"


def _save_file_version(user_id, filename, keep=EDIT_VERSIONS_KEEP):
    """Копия текущего файла в .versions перед перезаписью; храним последние keep."""
    src = os.path.join(_docs_dir(user_id), filename)
    if not os.path.isfile(src):
        return None
    vdir = _versions_dir(user_id)
    os.makedirs(vdir, exist_ok=True)
    version = f"{filename}.{time.strftime('%Y%m%d-%H%M%S')}"
    dst = os.path.join(vdir, version)
    shutil.copy2(src, dst)
    os.utime(dst, None)   # mtime = момент сохранения (для порядка чистки)
    prefix = filename + '.'
    versions = sorted(v for v in os.listdir(vdir)
                      if v.startswith(prefix) and os.path.isfile(os.path.join(vdir, v)))
    for old in versions[:-keep]:
        try:
            os.remove(os.path.join(vdir, old))
        except OSError:
            pass
    return version


def _count_csv_rows(file_path):
    """Сколько строк данных в CSV — для отчёта «N позиций из M строк»."""
    try:
        return sum(1 for row in _iter_csv_rows(file_path) if any(str(c).strip() for c in row))
    except Exception as e:
        logger.error(f"Ошибка подсчёта строк CSV {file_path}: {e}")
        return 0


def _after_text_edit(user_id, doc, filename, file_path):
    """После правки: группа по новому содержимому, ре-парсинг прайса, индексация."""
    info = {'doc_group': doc.get('doc_group') or '', 'is_price_list': False,
            'price_count': 0, 'price_total': 0, 'price_lost': False}
    is_price = False

    if filename.lower().endswith('.csv'):
        try:
            rows = parse_price_list(file_path)
        except Exception as e:
            logger.error(f"❌ Ошибка разбора прайс-листа после правки {filename}: {e}")
            rows = []
        if rows:
            is_price = True
            replace_price_items(user_id, filename, rows)
            info['is_price_list'] = True
            info['price_count'] = len(rows)
            info['price_total'] = _count_csv_rows(file_path)
            update_document_group(doc['id'], user_id, '💲 Прайс-листы')
            info['doc_group'] = '💲 Прайс-листы'
            logger.info(f"📋 Прайс-лист {filename} переразобран после правки: {len(rows)} позиций")
        elif filename in get_price_files(user_id):
            # Таблица сломана правкой: старые позиции устарели — снимаем их из поиска.
            delete_price_items_for_file(user_id, filename)
            info['price_lost'] = True
            logger.warning(f"⚠️ {filename} больше не распознаётся как прайс-лист: позиции сняты")

    if not is_price:
        group = detect_doc_group(filename, _read_text_preview(file_path), False)
        if group != (doc.get('doc_group') or ''):
            update_document_group(doc['id'], user_id, group)
        info['doc_group'] = group

    if rag_ready:
        _reindex_user_async(user_id)
    return info


@app.route('/api/documents/<int:doc_id>/text')
def get_document_text(doc_id):
    """Текст документа для редактора (.txt/.csv, только свои файлы)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']
    doc = _get_owned_doc(user_id, doc_id)
    if not doc:
        return jsonify({'error': 'Документ не найден'}), 404

    filename = os.path.basename(doc['filename'])
    if not _is_editable(filename):
        return jsonify({'error': 'Этот формат нельзя править как текст — только скачать и загрузить заново'}), 400

    path = os.path.join(_docs_dir(user_id), filename)
    if not os.path.isfile(path):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    size = os.path.getsize(path)
    if size > MAX_EDIT_BYTES:
        return jsonify({'error': f'Файл больше {MAX_EDIT_BYTES // (1024 * 1024)} МБ — правка в браузере недоступна, скачайте файл',
                        'too_big': True, 'size': size}), 413

    text, encoding, newline = _read_text_file(path)

    # txT-дубль для эмбеддингов: рядом лежит одноимённый файл другого формата,
    # и по ссылке из чата скачается ИМЕННО ОН (см. download_kb_file).
    is_rag_helper = False
    if filename.lower().endswith('.txt'):
        stem = os.path.splitext(filename)[0].lower()
        folder = _docs_dir(user_id)
        is_rag_helper = any(
            f.lower() != filename.lower() and os.path.splitext(f)[0].lower() == stem
            and not f.lower().endswith('.txt') and os.path.isfile(os.path.join(folder, f))
            for f in os.listdir(folder)
        )

    price_files = {}
    try:
        price_files = get_price_files(user_id)
    except Exception as e:
        logger.error(f"Ошибка определения прайс-листов: {e}")

    return jsonify({
        'success': True,
        'id': doc_id,
        'filename': doc['original_name'],
        'text': text,
        'etag': _doc_etag(path),
        'encoding': encoding,
        'newline': 'CRLF' if newline == '\r\n' else 'LF',
        'size': size,
        'is_rag_helper': is_rag_helper,
        'is_price_list': filename in price_files,
        'price_count': price_files.get(filename, 0),
    })


@app.route('/api/documents/<int:doc_id>/save', methods=['POST'])
def save_document_text(doc_id):
    """Сохранение текста из редактора: версия в .versions → запись → переиндексация."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not isinstance(text, str):
        return jsonify({'error': 'Не передан текст документа'}), 400

    doc = _get_owned_doc(user_id, doc_id)
    if not doc:
        return jsonify({'error': 'Документ не найден'}), 404

    filename = os.path.basename(doc['filename'])
    if not _is_editable(filename):
        return jsonify({'error': 'Этот формат нельзя править как текст'}), 400

    path = os.path.join(_docs_dir(user_id), filename)
    if not os.path.isfile(path):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    # Конфликт: файл менялся в другой вкладке/сессии после открытия редактора
    current_etag = _doc_etag(path)
    client_etag = data.get('etag')
    if client_etag and client_etag != current_etag:
        logger.warning(f"⚠️ Конфликт правки {filename} (user {user_id}): {client_etag} != {current_etag}")
        return jsonify({'error': 'Файл уже изменён в другой вкладке или сессии. Перезагрузите текст, чтобы не потерять чужие правки.',
                        'conflict': True, 'etag': current_etag}), 409

    _, encoding, newline = _read_text_file(path)
    payload = _restore_newlines(text, newline)
    try:
        encoded = payload.encode(encoding)
    except UnicodeEncodeError as e:
        bad = payload[e.start:e.end]
        return jsonify({'error': f'Символ «{bad}» нельзя записать в кодировке {encoding}: файл был загружен в ней. '
                                 f'Уберите символ или загрузите файл заново в UTF-8.'}), 400

    if len(encoded) > MAX_EDIT_BYTES:
        return jsonify({'error': f'После правки файл больше {MAX_EDIT_BYTES // (1024 * 1024)} МБ — сохранение отменено'}), 413

    version = _save_file_version(user_id, filename)
    try:
        _write_text_file(path, payload, encoding)
    except OSError as e:
        logger.error(f"❌ Не удалось записать {filename} (user {user_id}): {e}")
        return jsonify({'error': f'Не удалось записать файл: {e}'}), 500

    info = _after_text_edit(user_id, doc, filename, path)
    logger.info(f"✏️ Пользователь {session.get('username')} сохранил правку документа: {filename}")

    message = 'Файл сохранён'
    if info['is_price_list']:
        message += f". Прайс-лист переразобран: распознано {info['price_count']} позиций из {info['price_total']} строк"
    if info['price_lost']:
        message += '. ⚠️ Таблица с ценами больше не распознаётся — прежние позиции сняты из поиска'
    if rag_ready:
        message += '. Индексация базы знаний запущена в фоне'

    return jsonify({'success': True, 'message': message, 'etag': _doc_etag(path),
                    'version': version, 'encoding': encoding, **info})


@app.route('/api/documents/<int:doc_id>/versions')
def list_document_versions(doc_id):
    """Список сохранённых версий файла (последние EDIT_VERSIONS_KEEP)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']
    doc = _get_owned_doc(user_id, doc_id)
    if not doc:
        return jsonify({'error': 'Документ не найден'}), 404

    filename = os.path.basename(doc['filename'])
    if not _is_editable(filename):
        return jsonify({'error': 'Этот формат нельзя править как текст'}), 400

    vdir = _versions_dir(user_id)
    prefix = filename + '.'
    versions = []
    if os.path.isdir(vdir):
        for v in sorted(os.listdir(vdir), reverse=True):
            vpath = os.path.join(vdir, v)
            if not v.startswith(prefix) or not os.path.isfile(vpath):
                continue
            stamp = v[len(prefix):]
            try:
                saved_at = datetime.strptime(stamp, '%Y%m%d-%H%M%S').strftime('%d.%m.%Y %H:%M:%S')
            except ValueError:
                saved_at = stamp
            versions.append({'version': v, 'saved_at': saved_at, 'size': os.path.getsize(vpath)})
    return jsonify({'success': True, 'versions': versions})


@app.route('/api/documents/<int:doc_id>/versions/restore', methods=['POST'])
def restore_document_version(doc_id):
    """Откат файла к сохранённой версии (текущее состояние тоже сохраняется)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    user_id = session['user_id']
    doc = _get_owned_doc(user_id, doc_id)
    if not doc:
        return jsonify({'error': 'Документ не найден'}), 404

    filename = os.path.basename(doc['filename'])
    if not _is_editable(filename):
        return jsonify({'error': 'Этот формат нельзя править как текст'}), 400

    data = request.get_json(silent=True) or {}
    version = os.path.basename(str(data.get('version') or ''))
    vdir = _versions_dir(user_id)
    vpath = os.path.join(vdir, version)
    # Берём только имя из списка версий этого файла: защита от path traversal
    if not version.startswith(filename + '.') or not os.path.isfile(vpath):
        return jsonify({'error': 'Версия не найдена'}), 404

    path = os.path.join(_docs_dir(user_id), filename)
    if not os.path.isfile(path):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    _save_file_version(user_id, filename)   # откат тоже обратим
    shutil.copy2(vpath, path)
    info = _after_text_edit(user_id, doc, filename, path)
    logger.info(f"↩️ Пользователь {session.get('username')} откатил {filename} к версии {version}")

    return jsonify({'success': True, 'message': f'Файл восстановлен из версии {version}',
                    'etag': _doc_etag(path), **info})


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


# === Админка: модели LLM и OCR (страница /admin) ===
# Модель векторизации из админки НЕ переключается: она прошита в rag_core
# (_get_embedding_model), её смена требует полной переиндексации всех баз знаний —
# на странице она показывается справочно.
_ADMIN_SECRET_KEYS = ("llm_api_key", "llm_provider_api_key", "llm_openrouter_api_key")
_ADMIN_SECRET_LABELS = {
    "llm_api_key": "DashScope (он же для OCR)",
    "llm_provider_api_key": "DeepSeek",
    "llm_openrouter_api_key": "OpenRouter",
}


def _admin_models_view(settings):
    """Данные блока «Модели» для шаблона.

    Значения API-ключей НЕ отдаются в браузер даже админу: страница — это HTTP-ответ,
    секретам в нём не место. Вместо значения — признак «задан / не задан».
    """
    return {
        "values": {k: settings.get(k, "") for k in
                   ("llm_provider", "llm_model", "llm_base_url",
                    "ocr_model", "ocr_base_url", "ocr_dpi", "search_top_k")},
        "flags": {"disable_llm_models": bool(settings.get("disable_llm_models", False)),
                  "ocr_enabled": bool(settings.get("ocr_enabled", False))},
        "key_states": {k: ("задан" if str(settings.get(k, "") or "").strip() else "не задан")
                       for k in _ADMIN_SECRET_KEYS},
        "key_labels": _ADMIN_SECRET_LABELS,
        "providers": model_catalog.provider_names(),
        "catalog": model_catalog.PROVIDERS,
        "ocr_models": model_catalog.OCR_MODELS,
        "embedding_model": model_catalog.EMBEDDING_MODEL,
    }


def _admin_settings_updates(data):
    """Проверка JSON из админки → (updates, errors).

    Пустое поле ключа = «не менять»: так админ не затирает ключ случайно, а
    llm_api_key (общий с OCR) остаётся живым при смене LLM-провайдера.
    """
    updates, errors = {}, []
    provider = str(data.get("llm_provider", "") or "").strip()
    if provider not in model_catalog.PROVIDERS:
        errors.append(f"неизвестный провайдер LLM: {provider}")
    else:
        updates["llm_provider"] = provider
    for key, label in (("llm_model", "модель LLM"), ("ocr_model", "OCR-модель")):
        value = str(data.get(key, "") or "").strip()
        if not value:
            errors.append(f"{label} не может быть пустой")
        elif len(value) > 120 or any(c.isspace() for c in value):
            errors.append(f"{label}: недопустимое значение")
        else:
            updates[key] = value
    for key, label in (("llm_base_url", "LLM base_url"), ("ocr_base_url", "OCR base_url")):
        value = str(data.get(key, "") or "").strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            errors.append(f"{label}: нужен адрес вида https://…")
        else:
            updates[key] = value
    try:
        dpi = int(str(data.get("ocr_dpi", "")).strip())
    except (TypeError, ValueError):
        dpi = None
    if dpi is None or not 50 <= dpi <= 600:
        errors.append("OCR dpi: целое число 50–600")
    else:
        updates["ocr_dpi"] = dpi
    # top-K поиска — ОДНО значение на все модели (облачные и локальные). Больше
    # фрагментов = полнее контекст, но длиннее промпт: у локальной модели на GPU это
    # прямое время обработки промпта перед первым словом ответа.
    # Ключа нет в запросе — значение не трогаем (прежние клиенты шлют без него).
    if "search_top_k" in data:
        try:
            top_k = int(str(data.get("search_top_k", "")).strip())
        except (TypeError, ValueError):
            top_k = None
        if top_k is None or not 1 <= top_k <= 50:
            errors.append("top-K поиска: целое число 1–50")
        else:
            updates["search_top_k"] = top_k
    updates["ocr_enabled"] = bool(data.get("ocr_enabled"))
    updates["disable_llm_models"] = bool(data.get("disable_llm_models"))
    for key in _ADMIN_SECRET_KEYS:
        value = str(data.get(key, "") or "").strip()
        if not value:      # пусто — ключ не трогаем
            continue
        if len(value) < 8 or any(c.isspace() for c in value):
            errors.append(f"ключ ({_ADMIN_SECRET_LABELS[key]}): похоже на опечатку — "
                          f"короче 8 символов или с пробелом")
            continue
        updates[key] = value
    return updates, errors


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
        return render_template('admin.html', mode='dashboard', users=users,
                               models=_admin_models_view(get_all_settings()))
    return render_template('admin.html', mode='login')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    """Выход из админ-панели (пользовательская сессия не трогается)."""
    session.pop('admin', None)
    return redirect(url_for('admin'))


@app.route('/admin/api/settings', methods=['POST'])
def admin_save_settings():
    """Сохранение моделей LLM/OCR из админ-панели (только админ).

    Настройки общие для всех пользователей (таблица app_settings). Сразу после
    записи перечитываем их в этом же процессе: веб-сервер применяет новую модель
    со следующего запроса, перезапуск не нужен (ask_model() тоже делает reload()).
    """
    if not session.get('admin'):
        return jsonify({'error': 'Доступ запрещён'}), 403
    data = request.get_json(silent=True) or {}
    updates, errors = _admin_settings_updates(data)
    if errors:
        return jsonify({'error': '; '.join(errors)}), 400
    if not set_settings(updates):
        return jsonify({'error': 'Не удалось сохранить настройки в БД'}), 500
    try:
        from rag_core import settings as rag_settings
        rag_settings.reload()   # чтобы OCR/LLM в этом процессе увидели новые значения сразу
    except Exception as e:
        logger.error(f"Настройки сохранены, но не перечитаны в процессе: {e}")
    changed = [k for k in updates if k not in _ADMIN_SECRET_KEYS]
    secrets = [k for k in updates if k in _ADMIN_SECRET_KEYS]
    logger.info(f"⚙️ Админ изменил настройки моделей: {', '.join(changed)}"
                + (f" (+ключи: {', '.join(secrets)})" if secrets else ""))
    message = "Настройки сохранены и применены"
    if secrets:
        message += " (ключи обновлены, значения не показываются)"
    return jsonify({'success': True, 'message': message, 'changed': changed})


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



# ============================================================
# Сайт-виджет: чат-бот для встраивания на сайт клиента
# ============================================================
# Модель: на сайт клиента ставится <script src="https://.../widget.js" data-key="КЛЮЧ">.
# Загрузчик рисует плавающий бабл; по клику открывается iframe на /widget/<key>.
# Вся переписка идёт ВНУТРИ iframe — то есть это same-origin запросы к /api/widget/*:
# не нужны ни CORS, ни third-party cookies (которые режут Safari/Chrome).
# Идентичность посетителя — visitor_id в localStorage iframe; история каждого
# посетителя хранится в chat_history с device_id='wid:<widget>:<visitor>'.
# Ключ виджета = настоящий контроль доступа; allowed_domains — мягкая проверка
# (заявленный родительский Origin): отсекает случайное «скопировали и вставили»,
# но не злоумышленника с curl — поэтому дневной лимит запросов обязателен.

_WIDGET_SOURCES_RE = re.compile(r'\n*Источники:.*$', re.S)
_WIDGET_HEX_RE = re.compile(r'#[0-9a-fA-F]{6}')


def _visitor_answer(user_id, question, scope, label, bot_label='Бот', strip_sources=True):
    """Единый путь ответа внешнему собеседнику: виджет на сайте и чат-бот в MAX.

    Диалог изолирован по device_id=scope — у каждого собеседника своя лента,
    без fallback на основной чат владельца ('web').
    Возвращает (answer, err): при ошибке answer=None, err — текст для собеседника.
    """
    try:
        user_rag = get_user_rag(user_id)
        provider = user_rag.settings.get("llm_provider", "")
        model = user_rag.settings.get("llm_model", "")
        save_message(user_id, 'user', question, device_id=scope)
        chat_logger.log_message(label, user_id, question, is_bot=False,
                                provider=provider, model=model)
        history = _chat_context(user_id, scope, external=True)
        answer = user_rag.ask_model(question, user_prompt=get_user_prompt(user_id), history=history)
        # Внешнему собеседнику источники не показываем (файлы базы — внутренние)
        if strip_sources:
            answer = _WIDGET_SOURCES_RE.sub('', answer or '').strip()
        if not answer:
            answer = 'Не нашёл ответа в базе знаний. Попробуйте переформулировать вопрос.'
        save_message(user_id, 'assistant', answer, device_id=scope)
        chat_logger.log_message(bot_label, user_id, answer, is_bot=True,
                                provider=provider, model=model)
        # Аналитика общая с владельцем: он видит, что спрашивают собеседники, и пробелы в базе
        save_query_analytics(user_id, question, answer)
        return answer, None
    except Exception as e:
        logger.exception(f"Ошибка ответа собеседнику ({label}): {e}")
        return None, 'Ассистент временно недоступен. Попробуйте ещё раз.'


def _widget_domain_ok(w, site_origin):
    """Мягкая проверка домена: пустой список = разрешено любое размещение.

    Поддерживаются точные хосты (shop.ru) и поддомены (*.shop.ru).
    """
    raw = (w.get('allowed_domains') or '').replace(',', '\n')
    allowed = [d.strip().lower() for d in raw.split('\n') if d.strip()]
    if not allowed:
        return True
    if not site_origin:
        return False
    from urllib.parse import urlparse
    host = (urlparse(str(site_origin)).hostname or '').lower()
    if not host:
        return False
    for d in allowed:
        d = d.replace('https://', '').replace('http://', '').strip('/').lstrip('*.')
        if d and (host == d or host.endswith('.' + d)):
            return True
    return False


def _widget_or_404(key):
    w = get_widget_by_key((key or '').strip())
    if not w or not w.get('active'):
        abort(404)
    return w


def _widget_scope(w, visitor):
    """device_id посетителя виджета: своя изолированная история чата."""
    return ('wid:%s:%s' % (w['id'], visitor))[:64]


def _widget_guard(data):
    """Общие проверки запроса от iframe виджета: (w, visitor, err_response)."""
    key = (data.get('key') or '').strip()
    w = get_widget_by_key(key)
    if not w or not w.get('active'):
        return None, None, (jsonify({'error': 'Виджет не найден.'}), 404)
    visitor = data.get('visitor_id') or ''
    if not re.fullmatch(r'[0-9a-zA-Z_-]{8,64}', visitor):
        return None, None, (jsonify({'error': 'Некорректный посетитель.'}), 400)
    if not _widget_domain_ok(w, data.get('site') or ''):
        return None, None, (jsonify({'error': 'Виджет не настроен для этого сайта.'}), 403)
    return w, visitor, None


# --- Публичные эндпоинты виджета (без логина; доступ = ключ виджета) ---

@app.route('/widget.js')
def widget_loader():
    """JS-загрузчик встраиваемого виджета (ставится на сайт клиента)."""
    try:
        with open(os.path.join(app.static_folder, 'widget.js'), 'r', encoding='utf-8') as f:
            body = f.read()
    except OSError:
        abort(404)
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/api/widget/meta')
def widget_meta():
    """Публичная карточка виджета для загрузчика: название и цвет бабла."""
    w = _widget_or_404(request.args.get('key'))
    return jsonify({'name': w['name'], 'color': w.get('theme_color') or '#667eea'})


@app.route('/widget/<key>')
def widget_page(key):
    """Страница чата внутри iframe виджета."""
    w = _widget_or_404(key)
    site = (request.args.get('o') or '')[:200]
    if not _widget_domain_ok(w, site):
        return make_response(
            render_template('widget_frame.html', error='Этот ассистент не настроен для данного сайта.'),
            403)
    greeting = build_greeting(get_user_prompt(w['user_id']), 'гость')
    resp = make_response(render_template('widget_frame.html', w=w, key=w['key'], site=site, greeting=greeting))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/widget/history')
def widget_history_api():
    """История чата конкретного посетителя виджета."""
    w, visitor, err = _widget_guard(request.args)
    if err:
        return err
    scope = _widget_scope(w, visitor)
    started = get_session_start(w['user_id'], scope)
    return jsonify({'messages': _with_ts(get_widget_history(w['user_id'], scope)),
                    'session_started_at': started.timestamp() if started else 0})


@app.route('/api/widget/ask', methods=['POST'])
def widget_ask():
    """Вопрос посетителя сайта через виджет (доступ по ключу, без логина)."""
    data = request.get_json(silent=True) or {}
    w, visitor, err = _widget_guard(data)
    if err:
        return err
    question = (data.get('question') or '').strip()[:2000]
    if not question:
        return jsonify({'error': 'Пустой вопрос'}), 400
    # 10 сообщений/мин на посетителя + дневной лимит на виджет (за каждым — платный LLM-вызов)
    if _rate_limited('wg:%s:%s' % (w['key'], visitor), limit=10, window=60):
        return jsonify({'error': 'Слишком много сообщений. Подождите минуту.'}), 429
    if not widget_consume(w['id']):
        return jsonify({'answer': 'К сожалению, дневной лимит вопросов ассистенту исчерпан. Попробуйте завтра.'})
    if not rag_ready:
        return jsonify({'answer': 'Ассистент ещё просыпается. Попробуйте через минуту...'})
    answer, err_msg = _visitor_answer(w['user_id'], question, _widget_scope(w, visitor),
                                      "Виджет «%s»" % w['name'], bot_label='Бот (виджет)')
    if err_msg:
        logger.error(f"Виджет «{w['name']}»: {err_msg}")
        return jsonify({'error': err_msg}), 500
    return jsonify({'answer': answer})


@app.route('/api/widget/new', methods=['POST'])
def widget_new_dialogue():
    """Посетитель виджета начинает новый диалог: контекст модели очищается, лента остаётся."""
    data = request.get_json(silent=True) or {}
    w, visitor, err = _widget_guard(data)
    if err:
        return err
    ok = start_new_chat_session(w['user_id'], _widget_scope(w, visitor))
    return jsonify({'success': bool(ok)})


@app.route('/api/widget/clear', methods=['POST'])
def widget_clear():
    """Посетитель очищает свою историю чата в виджете."""
    data = request.get_json(silent=True) or {}
    w, visitor, err = _widget_guard(data)
    if err:
        return err
    return jsonify({'success': bool(clear_widget_history(w['user_id'], _widget_scope(w, visitor)))})


# --- Кабинет виджетов (страница /widgets и API — нужен логин владельца) ---

@app.route('/widgets')
def widgets_page():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('widgets.html', username=session.get('username', ''))


def _widget_sanitize_fields(body):
    """Белый список полей + валидация (возвращает (fields, ошибка))."""
    out = {}
    if 'name' in body:
        name = str(body.get('name') or '').strip()[:100]
        if not name:
            return None, 'Название не может быть пустым'
        out['name'] = name
    if 'allowed_domains' in body:
        out['allowed_domains'] = str(body.get('allowed_domains') or '').strip().lower()[:500]
    if 'daily_limit' in body:
        try:
            limit = int(body.get('daily_limit'))
        except (TypeError, ValueError):
            return None, 'Дневной лимит — целое число'
        out['daily_limit'] = max(1, min(limit, 10000))
    if 'theme_color' in body:
        c = str(body.get('theme_color') or '').strip()
        if not _WIDGET_HEX_RE.fullmatch(c):
            return None, 'Цвет должен быть в формате #RRGGBB'
        out['theme_color'] = c
    if 'active' in body:
        out['active'] = bool(body.get('active'))
    return out, None


@app.route('/api/widgets')
def widgets_list():
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    return jsonify({'widgets': list_user_widgets(session['user_id'])})


@app.route('/api/widgets/create', methods=['POST'])
def widgets_create():
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    body = request.get_json(silent=True) or {}
    fields, err = _widget_sanitize_fields({'name': body.get('name', ''),
                                          'allowed_domains': body.get('allowed_domains', ''),
                                          'daily_limit': body.get('daily_limit', 200),
                                          'theme_color': body.get('theme_color', '#667eea')})
    if err:
        return jsonify({'error': err}), 400
    ok, w = create_widget(session['user_id'], fields['name'],
                          allowed_domains=fields['allowed_domains'],
                          daily_limit=fields['daily_limit'],
                          theme_color=fields['theme_color'])
    if not ok:
        return jsonify({'error': 'Не удалось создать виджет'}), 500
    return jsonify({'widget': w})


@app.route('/api/widgets/<int:widget_id>', methods=['POST'])
def widgets_update(widget_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    body = request.get_json(silent=True) or {}
    fields, err = _widget_sanitize_fields(body)
    if err:
        return jsonify({'error': err}), 400
    if not fields:
        return jsonify({'error': 'Нет изменений'}), 400
    if not update_widget(session['user_id'], widget_id, fields):
        return jsonify({'error': 'Виджет не найден или ошибка сохранения'}), 404
    return jsonify({'success': True})


@app.route('/api/widgets/delete/<int:widget_id>', methods=['POST'])
def widgets_delete(widget_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    if not delete_widget(session['user_id'], widget_id):
        return jsonify({'error': 'Виджет не найден'}), 404
    return jsonify({'success': True})



# ============================================================
# Канал MAX: тот же ассистент в мессенджере MAX (max.ru)
# ============================================================
# Личный бот пользователя: токен владелец вводит сам в кабинете (🤖 MAX-бот),
# хранится он в PG (таблица max_channels), а не в файлах проекта.
# События приходят вебхуком POST /max/hook/<hook_key> с секретом в заголовке
# X-Max-Bot-Api-Secret. MAX ждёт ответ 200 в течение 30 секунд и повторяет
# доставку до 10 раз — поэтому обработчик лишь ставит задачу в очередь, считает
# её воркер, а повторные события отсекает дедупликация (max_bot.is_duplicate).
# Переписка каждого собеседника — своя область chat_history:
# device_id='max:<channel_id>:<max_user_id>'; источники файлов наружу не отдаём.
# Если MAX не принял вебхук (нет публичного HTTPS), канал переходит на long
# polling GET /updates — запасной транспорт, документация MAX его для прода не
# рекомендует, поэтому он включается только как резерв.

_max_queue = None
_max_polling = None


def _norm_command(text):
    """Нормализация текстовой команды собеседника («Новый диалог!» → 'новый диалог')."""
    return ' '.join((text or '').strip().lower().rstrip('!.,').split())


def _max_scope(channel_id, max_user_id):
    """Область истории собеседника в MAX: у каждого пользователя своя лента."""
    return ('max:%s:%s' % (channel_id, max_user_id))[:64]


def _public_base_url():
    """Публичный адрес сервиса. Внутренний запрос от Caddy идёт по http,
    поэтому схему берём из X-Forwarded-Proto (иначе считаем https)."""
    proto = (request.headers.get('X-Forwarded-Proto') or 'https').split(',')[0].strip() or 'https'
    host = request.headers.get('X-Forwarded-Host') or request.host
    return '%s://%s' % (proto, host)


def _max_hook_url(channel):
    return _public_base_url() + '/max/hook/' + (channel.get('hook_key') or '')


def _max_send(channel, text, max_user_id):
    """Отправить ответ собеседнику в MAX (длинный текст режется на части)."""
    ok, err = max_bot.send_text(channel.get('token') or '', text, user_id=max_user_id)
    if not ok:
        logger.warning(f"MAX: сообщение не доставлено (канал #{channel.get('id')}): {err}")
    return ok


def _max_handle_update(channel, update):
    """Обработка события MAX (в воркере): вопрос собеседника → ответ из базы знаний."""
    utype = (update.get('update_type') or '').strip()
    message = update.get('message') or {}
    sender = (message.get('sender') or {}).get('user_id')
    text = ((message.get('body') or {}).get('text') or '').strip()
    if not sender:
        return
    if utype == 'bot_started':
        _max_send(channel, build_greeting(get_user_prompt(channel['user_id']), 'гость'), sender)
        return
    if utype != 'message_created' or not text:
        return
    # Команда «новый диалог»: контекст модели очищается, ответа от LLM не требуется
    if _norm_command(text) in ('/new', '/новый', '/начать', 'новый диалог', 'начать заново'):
        start_new_chat_session(channel['user_id'], _max_scope(channel['id'], sender))
        _max_send(channel, 'Начали новый диалог. Слушаю ваш вопрос.', sender)
        return
    # 10 сообщений/мин на собеседника (каждый ответ — платный вызов LLM)
    if _rate_limited('mx:%s:%s' % (channel['id'], sender), limit=10, window=60):
        logger.info(f"MAX: слишком часто пишет пользователь {sender} (канал #{channel['id']})")
        return
    if not max_consume(channel['id']):
        _max_send(channel, 'К сожалению, дневной лимит ответов ассистента исчерпан. Попробуйте завтра.', sender)
        return
    if not rag_ready:
        _max_send(channel, 'Ассистент ещё просыпается. Попробуйте через минуту...', sender)
        return
    answer, err = _visitor_answer(channel['user_id'], text[:2000], _max_scope(channel['id'], sender),
                                  "MAX «%s»" % (channel.get('bot_name') or 'бот'),
                                  bot_label='Бот (MAX)')
    _max_send(channel, answer or err or 'Не получилось ответить. Попробуйте ещё раз.', sender)


@app.route('/max/hook/<hook_key>', methods=['GET', 'POST'])
def max_webhook(hook_key):
    """Вебхук MAX: принимаем событие, сразу отвечаем 200, обрабатываем в фоне.

    GET поддерживается не для событий: MAX проверяет адрес подписки запросом
    без тела (в логах видно такие проверки), поэтому отвечаем коротким 200.
    """
    channel = get_max_channel_by_hook((hook_key or '').strip())
    if not channel:
        abort(404)
    if not channel.get('active'):
        # Бот выключен владельцем: событие не обрабатываем, но отвечаем 200 —
        # иначе MAX будет повторять доставку до 10 раз (до 8 часов) без пользы.
        logger.info(f"MAX: событие проигнорировано, бот выключен (канал #{channel.get('id')})")
        return jsonify({'ok': True, 'skipped': 'bot_disabled'})
    if request.method == 'GET':
        return jsonify({'ok': True, 'bot': channel.get('bot_username') or ''})
    secret = request.headers.get('X-Max-Bot-Api-Secret', '')
    if not channel.get('hook_secret') or secret != channel['hook_secret']:
        logger.warning(f"MAX: вебхук с неверным секретом отклонён (канал #{channel.get('id')})")
        abort(403)
    update = request.get_json(silent=True) or {}
    if _max_queue is not None:
        _max_queue.put(channel, update)
    return jsonify({'ok': True})


def _max_supervisor():
    """Раз в минуту: держим long polling только у каналов с запасным транспортом."""
    time.sleep(20)
    while True:
        try:
            if _max_polling is not None:
                for channel in list_active_max_channels():
                    if (channel.get('mode') or 'webhook') == 'polling':
                        _max_polling.ensure(channel)
        except Exception as e:
            logger.error(f"MAX: ошибка супервизора каналов: {e}")
        time.sleep(60)


def _max_status(channel):
    """Состояние канала для кабинета (полный токен отдаёт отдельный эндпоинт)."""
    if not channel:
        return {'connected': False}
    token = channel.get('token') or ''
    return {
        'connected': True,
        'bot_name': channel.get('bot_name') or '',
        'bot_username': channel.get('bot_username') or '',
        'bot_user_id': channel.get('bot_user_id'),
        'mode': channel.get('mode') or 'webhook',
        'active': bool(channel.get('active')),
        'daily_limit': channel.get('daily_limit') or 0,
        'today_hits': channel.get('today_hits') or 0,
        'total_requests': channel.get('total_requests') or 0,
        'last_used_at': str(channel.get('last_used_at') or ''),
        'token_masked': (token[:6] + chr(8230) + token[-4:]) if len(token) > 12 else chr(8230),
        'hook_url': _max_hook_url(channel),
        'bot_url': ('https://max.ru/' + channel['bot_username']) if channel.get('bot_username') else '',
    }


@app.route('/api/max')
def max_status_api():
    """Состояние подключения MAX для кабинета."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    return jsonify(_max_status(get_max_channel(session['user_id'])))


@app.route('/api/max/token')
def max_token_api():
    """Полный токен своего бота — по явному запросу кабинета (кнопка «показать»)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    channel = get_max_channel(session['user_id'])
    if not channel:
        return jsonify({'error': 'Бот не подключён'}), 404
    return jsonify({'token': channel.get('token') or ''})


@app.route('/api/max/connect', methods=['POST'])
def max_connect():
    """Подключение личного MAX-бота: проверка токена, подписка на вебхук, сохранение."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    user_id = session['user_id']
    body = request.get_json(silent=True) or {}
    channel = get_max_channel(user_id)
    token = (body.get('token') or '').strip()
    if not token and channel:
        token = channel.get('token') or ''      # пустое поле = оставляем текущий токен
    if not token:
        return jsonify({'error': 'Вставьте токен бота: MAX для партнёров → Чат-боты → ⋮ → Настройки'}), 400
    ok, me = max_bot.get_me(token)
    if not ok:
        return jsonify({'error': 'MAX не принял токен: %s' % (me.get('error') or 'неизвестная ошибка')}), 400
    hook_key = (channel or {}).get('hook_key') or secrets.token_urlsafe(16)
    hook_secret = (channel or {}).get('hook_secret') or secrets.token_urlsafe(24)
    limit = (channel or {}).get('daily_limit') or 200
    ok_save, saved = save_max_channel(user_id, token, me.get('user_id'), me.get('name') or '',
                                      me.get('username') or '', hook_key, hook_secret, limit)
    if not ok_save or not saved:
        return jsonify({'error': 'Не удалось сохранить подключение'}), 500
    hook_url = _public_base_url() + '/max/hook/' + hook_key
    ok_hook, sub = max_bot.subscribe(token, hook_url, hook_secret)
    update_max_channel(user_id, {'mode': 'webhook' if ok_hook else 'polling', 'active': True})
    if _max_polling is not None:
        if ok_hook:
            _max_polling.stop(saved['id'])
        else:
            _max_polling.ensure(dict(saved, mode='polling'))
    result = _max_status(get_max_channel(user_id))
    if ok_hook:
        result['message'] = 'Бот подключён: %s (@%s)' % (result['bot_name'], result['bot_username'])
    else:
        result['warning'] = ('MAX не принял вебхук (%s) — включён режим опроса (long polling). '
                             'Для вебхука нужен публичный HTTPS-адрес на порту 443.'
                             % (sub.get('error') or 'ошибка подписки'))
    return jsonify(result)


def _max_sanitize_fields(body):
    """Белый список полей канала из кабинета."""
    out = {}
    if 'daily_limit' in body:
        try:
            out['daily_limit'] = max(1, min(int(body.get('daily_limit')), 10000))
        except (TypeError, ValueError):
            return None, 'Дневной лимит — целое число'
    if 'active' in body:
        out['active'] = bool(body.get('active'))
    return out, None


@app.route('/api/max/update', methods=['POST'])
def max_update():
    """Настройки канала: дневной лимит и включение бота."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    body = request.get_json(silent=True) or {}
    fields, err = _max_sanitize_fields(body)
    if err:
        return jsonify({'error': err}), 400
    if not fields:
        return jsonify({'error': 'Нет изменений'}), 400
    if not update_max_channel(session['user_id'], fields):
        return jsonify({'error': 'Бот не подключён'}), 404
    return jsonify({'success': True})


@app.route('/api/max/disconnect', methods=['POST'])
def max_disconnect():
    """Отключение: снимаем подписку MAX и удаляем подключение."""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401
    channel = get_max_channel(session['user_id'])
    if not channel:
        return jsonify({'error': 'Бот не подключён'}), 404
    try:
        max_bot.unsubscribe(channel.get('token') or '', _max_hook_url(channel))
    except Exception as e:
        logger.warning(f"MAX: не удалось снять подписку: {e}")
    if _max_polling is not None:
        _max_polling.stop(channel['id'])
    delete_max_channel(session['user_id'])
    return jsonify({'success': True})


def create_app():
    """Создание Flask приложения"""
    # Инициализируем БД аутентификации
    init_auth()

    # Канал MAX: очередь ответов (вебхук только принимает события) + резервный опрос
    global _max_queue, _max_polling
    _max_queue = max_bot.UpdateQueue(_max_handle_update)
    _max_queue.start()
    _max_polling = max_bot.PollingManager(_max_handle_update)
    threading.Thread(target=_max_supervisor, daemon=True).start()

    # Запускаем инициализацию RAG-системы в отдельном потоке
    init_thread = threading.Thread(target=initialize_rag_system, daemon=True)
    init_thread.start()

    return app
