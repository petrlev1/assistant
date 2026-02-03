# web_app.py - Веб-интерфейс для RAG-системы
from flask import Flask, render_template, request, jsonify
import threading
import logging
import asyncio
from rag_core import get_rag_system, RAGSettings

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

@app.route('/')
def index():
    """Главная страница с веб-интерфейсом"""
    return render_template('index.html')

@app.route('/ask', methods=['POST'])
def ask_question():
    """Обработка вопроса от пользователя"""
    global rag_system
    
    try:
        # Получаем вопрос из запроса
        data = request.get_json()
        question = data.get('question', '').strip()
        
        if not question:
            return jsonify({'error': 'Пустой вопрос'}), 400
        
        # Проверяем, инициализирована ли система
        if rag_system is None:
            return jsonify({'answer': 'Система еще инициализируется. Пожалуйста, подождите...'}), 200
        
        # Получаем ответ от RAG-системы
        logger.info(f"Получен вопрос: {question}")
        answer = rag_system.ask_model(question)
        logger.info("Ответ сгенерирован успешно")
        
        return jsonify({'answer': answer})
        
    except Exception as e:
        logger.error(f"Ошибка обработки вопроса: {e}")
        return jsonify({'error': f'Ошибка обработки вопроса: {str(e)}'}), 500

@app.route('/status')
def status():
    """Проверка статуса системы"""
    global rag_system
    return jsonify({'initialized': rag_system is not None})


def _run_telegram_bot():
    """Запуск Telegram-бота в отдельном потоке (как в run_gui.py)"""
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
    """Статус Telegram-бота: запущен ли, есть ли сохранённый токен"""
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
    """Запуск Telegram-бота. Токен можно передать в body или использовать сохранённый."""
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
    # Запускаем инициализацию RAG-системы в отдельном потоке
    init_thread = threading.Thread(target=initialize_rag_system, daemon=True)
    init_thread.start()
    
    return app

if __name__ == '__main__':
    # Создаем приложение и запускаем сервер
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)