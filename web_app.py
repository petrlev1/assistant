# web_app.py - Веб-интерфейс для RAG-системы (с авторизацией)
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
import threading
import logging
import asyncio
import os
from rag_core import get_rag_system, RAGSettings
from chat_logger import get_chat_logger
from auth_db import init_db, register_user, login_user

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация логгера чата
chat_logger = get_chat_logger()

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

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
    """Инициализация БД аутентификации"""
    try:
        init_db()
        logger.info("База данных аутентификации инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД аутентификации: {e}")


# === Маршруты аутентификации ===

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    if request.method == 'GET':
        return render_template('login.html')

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    success, result = login_user(username, password)
    if success:
        session['user_id'] = result['id']
        session['username'] = result['username']
        logger.info(f"Пользователь {username} вошёл в систему")
        return redirect(url_for('index'))
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
    """Главная страница с чатом (требуется авторизация)"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/ask', methods=['POST'])
def ask_question():
    """Обработка вопроса от пользователя (требуется авторизация)"""
    if 'user_id' not in session:
        return jsonify({'error': 'Необходима авторизация'}), 401

    global rag_system

    try:
        data = request.get_json()
        question = data.get('question', '').strip()
        user_name = session.get('username', 'Пользователь')

        if not question:
            return jsonify({'error': 'Пустой вопрос'}), 400

        if rag_system is None:
            return jsonify({'answer': 'Система еще инициализируется. Пожалуйста, подождите...'}), 200

        logger.info(f"Вопрос от {user_name}: {question}")

        # Логирование вопроса в файл чата
        chat_logger.log_message(user_name, 0, question, is_bot=False)

        answer = rag_system.ask_model(question)
        logger.info("Ответ сгенерирован успешно")

        # Логирование ответа
        chat_logger.log_message("Бот", 0, answer, is_bot=True)

        return jsonify({'answer': answer})

    except Exception as e:
        logger.error(f"Ошибка обработки вопроса: {e}")
        return jsonify({'error': f'Ошибка обработки вопроса: {str(e)}'}), 500


@app.route('/status')
def status():
    """Проверка статуса системы"""
    if 'user_id' not in session:
        return jsonify({'initialized': False}), 401
    global rag_system
    return jsonify({'initialized': rag_system is not None})


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