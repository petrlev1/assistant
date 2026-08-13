# web_app.py - Веб-интерфейс для RAG-системы (с авторизацией)
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_file
import threading
import logging
import asyncio
import os
from rag_core import get_rag_system, RAGSettings, DEFAULT_BASE_PROMPT, build_greeting, parse_price_list
from chat_logger import get_chat_logger
from auth_db import init_db, register_user, login_user, init_chat_history, save_message, get_history, add_document, delete_document, get_user_documents, clear_chat_history, delete_message, delete_message_pair, get_user_prompt, set_user_prompt, get_price_files, replace_price_items, delete_price_items_for_file

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

# Глобальная переменная для RAG-системы
rag_system = None

# Состояние Telegram-бота (общее с run_gui.py)
telegram_bot = None
telegram_thread = None
telegram_running = False


def initialize_rag_system():
    """Инициализация RAG-системы в отдельном потоке"""
    global rag_system
    try:
        logger.info("Запуск инициализации RAG-системы...")
        rag_system = get_rag_system()
        logger.info("RAG-система успешно инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации RAG-системы: {e}")


def init_auth():
    """Инициализация БД аутентификации и истории чата"""
    try:
        init_db()
        init_chat_history()
        logger.info("База данных аутентификации инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД аутентификации: {e}")


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
        # Загружаем базу знаний пользователя
        global rag_system
        if rag_system is not None:
            rag_system.load_for_user(session['user_id'])
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
    # После рестарта сервера сессия пользователя живёт, а RAG-система остаётся
    # на общей (пустой) базе знаний. Загружаем БЗ текущего пользователя, если ещё не загружена.
    global rag_system
    if rag_system is not None and rag_system.current_user_id != session['user_id']:
        rag_system.load_for_user(session['user_id'])
    # Приветствие формируется из персонального промта: бот представляется своей ролью
    user_prompt = get_user_prompt(session['user_id'])
    greeting = build_greeting(user_prompt, session.get('username', 'Пользователь'))
    return render_template('index.html', greeting=greeting)


@app.route('/about')
def about():
    """Публичная страница с описанием и возможностями системы (для клиентов)"""
    return render_template('about.html', logged_in='user_id' in session, username=session.get('username', ''))


@app.route('/ask', methods=['POST'])
def ask_question():
    """Обработка вопроса от пользователя (требуется авторизация)"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    global rag_system

    # Убеждаемся, что загружена БЗ текущего пользователя (актуально после рестарта сервера)
    if rag_system is not None and rag_system.current_user_id != session['user_id']:
        rag_system.load_for_user(session['user_id'])

    try:
        data = request.get_json()
        question = data.get('question', '').strip()
        user_id = session['user_id']
        user_name = session.get('username', 'Пользователь')

        if not question:
            return jsonify({'error': 'Пустой вопрос'}), 400

        if rag_system is None:
            return jsonify({'answer': 'Система еще инициализируется. Пожалуйста, подождите...'}), 200

        logger.info(f"Вопрос от {user_name}: {question}")

        # Сохраняем вопрос в историю
        user_msg_id = save_message(user_id, 'user', question)

        # Логирование вопроса в файл чата (с провайдером и моделью)
        provider = rag_system.settings.get("llm_provider", "")
        model = rag_system.settings.get("llm_model", "")
        chat_logger.log_message(user_name, user_id, question, is_bot=False, provider=provider, model=model)

        # Персональный промт пользователя (если задан — заменит системный промт по умолчанию)
        user_prompt = get_user_prompt(user_id)

        answer = rag_system.ask_model(question, user_prompt=user_prompt)
        logger.info("Ответ сгенерирован успешно")

        # Сохраняем ответ в историю
        assistant_msg_id = save_message(user_id, 'assistant', answer)

        # Логирование ответа
        chat_logger.log_message("Бот", user_id, answer, is_bot=True, provider=provider, model=model)

        return jsonify({'answer': answer, 'user_msg_id': user_msg_id, 'assistant_msg_id': assistant_msg_id})

    except Exception as e:
        logger.error(f"Ошибка обработки вопроса: {e}")
        return jsonify({'error': f'Ошибка обработки вопроса: {str(e)}'}), 500


@app.route('/history')
def get_chat_history():
    """Получение истории чата текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    messages = get_history(session['user_id'])
    return jsonify({'messages': messages})


@app.route('/api/chat/clear', methods=['POST'])
def clear_chat():
    """Очистка истории чата текущего пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    success = clear_chat_history(session['user_id'])
    if success:
        logger.info(f"🗑️ Пользователь {session.get('username')} очистил историю чата")
        return jsonify({'success': True, 'message': 'История чата очищена'})
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
    global rag_system
    return jsonify({'initialized': rag_system is not None})


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
    return jsonify({'documents': docs})


@app.route('/api/documents/upload', methods=['POST'])
def upload_document():
    """Загрузка документа в базу знаний пользователя"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    if 'file' not in request.files:
        return jsonify({'error': 'Файл не выбран'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Пустое имя файла'}), 400

    # Проверка расширения
    allowed_ext = ('.txt', '.pdf', '.docx', '.csv', '.xlsx', '.xls')
    if not file.filename.lower().endswith(allowed_ext):
        return jsonify({'error': f'Неподдерживаемый формат. Разрешены: {", ".join(allowed_ext)}'}), 400

    user_id = session['user_id']
    user_db_folder = os.path.join('Database', f'user_{user_id}')
    os.makedirs(user_db_folder, exist_ok=True)

    # Ручной флажок «Это прайс-лист» (если автодетект не сработал)
    is_price_flag = request.form.get('is_price_list') in ('1', 'true', 'on')

    # Сохраняем файл
    filename = file.filename
    file_path = os.path.join(user_db_folder, filename)
    file.save(file_path)

    # Добавляем запись в БД
    success, doc_id = add_document(user_id, filename, filename)
    if not success:
        # Если не удалось записать в БД — удаляем файл
        os.remove(file_path)
        return jsonify({'error': 'Ошибка при сохранении в БД'}), 500

    # Прайс-лист: автоопределение (xlsx/csv) или ручной флажок.
    # Парсер сам решает, похож ли файл на прайс (шапка с ценой+наименованием и есть строки с ценой).
    price_parsed = 0
    is_price_list = False
    if file.filename.lower().endswith(('.xlsx', '.csv')):
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
        elif is_price_flag:
            logger.warning(f"⚠️ {filename} помечен как прайс-лист, но парсер не нашёл таблицу с ценой и наименованием")

    # Перезагружаем базу знаний пользователя
    global rag_system
    if rag_system is not None:
        rag_system.load_for_user(user_id)

    logger.info(f"📄 Пользователь {session.get('username')} загрузил документ: {filename}")
    if is_price_list:
        message = f'Прайс-лист {filename} загружен: {price_parsed} позиций'
    else:
        message = f'Документ {filename} загружен'
    return jsonify({'success': True, 'message': message, 'is_price_list': is_price_list, 'price_count': price_parsed})


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

    # Перезагружаем базу знаний пользователя
    global rag_system
    if rag_system is not None:
        rag_system.load_for_user(user_id)

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
        logger.error(f"Ошибка запуска Telegram бота: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
        logger.error(f"Ошибка остановки Telegram бота: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def create_app():
    """Создание Flask приложения"""
    # Инициализируем БД аутентификации
    init_auth()

    # Запускаем инициализацию RAG-системы в отдельном потоке
    init_thread = threading.Thread(target=initialize_rag_system, daemon=True)
    init_thread.start()

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)