# chat_logger.py - Логирование переписки чат-бота
import logging
from datetime import datetime
from pathlib import Path

class ChatLogger:
    """Класс для логирования переписки чат-бота"""
    
    def __init__(self, log_dir="chat_logs"):
        """
        Инициализация логгера чата
        
        Args:
            log_dir: Путь к директории для логов
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Настройка логгера
        self.logger = logging.getLogger("chat_logger")
        self.logger.setLevel(logging.INFO)
        
        # Убираем старые обработчики, если есть
        self.logger.handlers.clear()
        
        # Создаём обработчик для файла (по дате)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = self.log_dir / f"chat_{today}.log"
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # Формат: дата и время в начале строки
        formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        
        # Также добавляем обработчик для консоли (опционально)
        # console_handler = logging.StreamHandler()
        # console_handler.setFormatter(formatter)
        # self.logger.addHandler(console_handler)
    
    def log_message(self, user_name, user_id, message, is_bot=False, chat_id=None):
        """
        Запись сообщения в лог
        
        Args:
            user_name: Имя пользователя
            user_id: ID пользователя
            message: Текст сообщения
            is_bot: Флаг, от бота сообщение или от пользователя
            chat_id: ID чата (опционально)
        """
        prefix = "🤖 Бот:" if is_bot else f"👤 {user_name} (ID: {user_id}):"
        if chat_id:
            prefix += f" [Chat: {chat_id}]"
        
        # Обработка многострочных сообщений
        message_text = message.replace('\n', '\\n')
        
        log_entry = f"{prefix} {message_text}"
        self.logger.info(log_entry)
    
    def log_command(self, user_name, user_id, command, chat_id=None):
        """
        Запись команды в лог
        
        Args:
            user_name: Имя пользователя
            user_id: ID пользователя
            command: Команда (без слэша)
            chat_id: ID чата (опционально)
        """
        prefix = f"👤 {user_name} (ID: {user_id}):"
        if chat_id:
            prefix += f" [Chat: {chat_id}]"
        
        log_entry = f"{prefix} /{command}"
        self.logger.info(log_entry)
    
    def log_error(self, user_name, user_id, error_message, context=None):
        """
        Запись ошибки в лог
        
        Args:
            user_name: Имя пользователя
            user_id: ID пользователя
            error_message: Описание ошибки
            context: Контекст ошибки (опционально)
        """
        prefix = f"❌ Ошибка от {user_name} (ID: {user_id}):"
        
        log_entry = f"{prefix} {error_message}"
        if context:
            log_entry += f" | Контекст: {context}"
        
        self.logger.error(log_entry)
    
    def get_today_log_file(self):
        """Возвращает путь к текущему файлу логов"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"chat_{today}.log"
    
    def rotate_logs(self):
        """
        Проверка и переключение лог-файла на новый день
        Вызывать в начале каждого дня или периодически
        """
        today = datetime.now().strftime("%Y-%m-%d")
        current_log = self.log_dir / f"chat_{today}.log"
        
        # Закрываем текущий обработчик
        for handler in self.logger.handlers:
            handler.close()
            self.logger.removeHandler(handler)
        
        # Создаём новый обработчик
        file_handler = logging.FileHandler(current_log, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)


# Глобальный экземпляр логгера
chat_logger = None

def get_chat_logger():
    """Получение глобального экземпляра логгера чата"""
    global chat_logger
    if chat_logger is None:
        chat_logger = ChatLogger()
    return chat_logger
