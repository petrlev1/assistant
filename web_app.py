# web_app.py - Веб-интерфейс для RAG-системы
from flask import Flask, render_template, request, jsonify
import threading
import logging
from rag_core import get_rag_system

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Глобальная переменная для RAG-системы
rag_system = None

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