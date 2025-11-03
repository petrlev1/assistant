# telegram_bot.py - Telegram бот для RAG-системы
import asyncio
import logging
import sys
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from rag_core import get_rag_system

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class TelegramRAGBot:
    def __init__(self, bot_token=None):
        """Инициализация Telegram-бота"""
        self.bot_token = bot_token
        self.rag_system = get_rag_system()
        self.application = None
        self.is_running = False
        
    def set_token(self, token):
        """Установка токена бота"""
        self.bot_token = token
        
    def is_token_valid(self):
        """Проверка валидности токена"""
        return self.bot_token and self.bot_token != "YOUR_BOT_TOKEN_HERE" and len(self.bot_token) > 20
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        welcome_message = (
            f"🌟 Привет, {user.first_name}! Добро пожаловать в RAG-бот компании ГТИ-ОПТ!\n\n"
            f"🤖 Я информационный ассистент, созданный на базе искусственного интеллекта.\n"
            f"💬 Просто задайте мне вопрос, и я постараюсь на него ответить, используя нашу базу знаний.\n\n"
            f"❓ Примеры вопросов:\n"
            f"• Какие услуги вы предоставляете?\n"
            f"• Кто является ответственным за проект X?\n"
            f"• Как связаться с отделом продаж?\n\n"
            f"Команды:\n"
            f"/start - Начальное сообщение\n"
            f"/help - Помощь\n"
            f"/stats - Статистика"
        )
        await update.message.reply_text(welcome_message)
        logger.info(f"Пользователь {user.first_name} (@{user.username}) начал диалог")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        help_text = (
            "📖 *Помощь по использованию бота:*\n\n"
            "🔹 *Как пользоваться:*\n"
            "• Просто отправьте текстовое сообщение с вашим вопросом\n"
            "• Бот проанализирует базу знаний и даст ответ\n\n"
            "🔹 *Поддерживаемые типы вопросов:*\n"
            "• Вопросы о компании и услугах\n"
            "• Запросы информации о проектах\n"
            "• Контактная информация сотрудников\n"
            "• Информация из документов компании\n\n"
            "🔹 *Команды:*\n"
            "/start - Начальное сообщение\n"
            "/help - Это сообщение помощи\n"
            "/stats - Статистика\n\n"
            "⚠ *Важно:*\n"
            "• Ответы формируются на основе имеющейся базы знаний\n"
            "• Если ответа нет в базе, бот сообщит об этом"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')
        logger.info(f"Пользователь {update.effective_user.first_name} запросил помощь")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stats"""
        user = update.effective_user
        stats_text = (
            "📊 *Статистика системы:*\n\n"
            f"📚 Фрагментов в базе знаний: {len(self.rag_system.my_knowledge)}\n"
            f"📁 Загружено файлов: {len(self.rag_system.all_knowledge_dict)}\n"
            f"🧠 Модель поиска: SentenceTransformer\n"
            f"🔤 Методы поиска: Гибридный (Semantic + BM25)"
        )
        await update.message.reply_text(stats_text, parse_mode='Markdown')
        logger.info(f"Пользователь {user.first_name} запросил статистику")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user_message = update.message.text
        user = update.effective_user
        
        logger.info(f"Получено сообщение от {user.first_name} (@{user.username}, ID: {user.id}): {user_message}")
        
        try:
            # Уведомление о том, что сообщение обрабатывается
            await update.message.chat.send_action("typing")
            
            # Обработка запроса через RAG-систему
            answer = await asyncio.get_event_loop().run_in_executor(
                None, self.rag_system.ask_model, user_message
            )
            
            # Форматирование ответа для Telegram
            if len(answer) > 4000:  # Ограничение Telegram на длину сообщения
                # Разбиваем длинный ответ на части
                chunks = [answer[i:i+4000] for i in range(0, len(answer), 4000)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await update.message.reply_text(f"📝 *Ответ (часть {i+1}/{len(chunks)}):*\n\n{chunk}", parse_mode='Markdown')
                    else:
                        await update.message.reply_text(f"📝 *Продолжение (часть {i+1}/{len(chunks)}):*\n\n{chunk}", parse_mode='Markdown')
            else:
                await update.message.reply_text(f"📝 *Ответ:*\n\n{answer}", parse_mode='Markdown')
            
            logger.info(f"Ответ отправлен пользователю {user.first_name}")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения от {user.first_name}: {e}", exc_info=True)
            error_text = (
                "❌ Произошла ошибка при обработке вашего запроса.\n\n"
                "Пожалуйста, попробуйте:\n"
                "1. Переформулировать вопрос\n"
                "2. Попробовать позже"
            )
            await update.message.reply_text(error_text)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик документов (временно не реализован)"""
        user = update.effective_user
        await update.message.reply_text(
            "📄 Спасибо за отправленный документ!\n\n"
            "⚠ *Внимание:* В текущей версии бот не может обрабатывать загруженные документы.\n"
            "Для добавления информации в базу знаний обратитесь к администратору системы."
        )
        logger.info(f"Пользователь {user.first_name} отправил документ (не обработан)")

    async def post_init(self, application: Application) -> None:
        """Инициализация бота после запуска"""
        # Установка команд бота
        await application.bot.set_my_commands([
            BotCommand("start", "Начальное сообщение"),
            BotCommand("help", "Помощь по использованию"),
            BotCommand("stats", "Статистика")
        ])
        logger.info("Команды бота установлены")
        self.is_running = True

    def run(self):
        """Запуск бота — ИСПРАВЛЕННАЯ ВЕРСИЯ"""
        if not self.is_token_valid():
            logger.error("❌ Не установлен валидный токен Telegram-бота")
            raise ValueError("Не установлен валидный токен Telegram-бота")
            
        try:
            # Создание приложения
            self.application = Application.builder().token(self.bot_token).post_init(self.post_init).build()

            # Регистрация обработчиков
            self.application.add_handler(CommandHandler("start", self.start))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("stats", self.stats_command))
            self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

            # Запуск бота БЕЗ обработчиков сигналов (ключевое исправление!)
            logger.info("Telegram-бот запущен. Ожидание сообщений...")
            
            # Вот главное исправление: stop_signals=[] отключает установку обработчиков сигналов
            self.application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=[]  # <-- ЭТО РЕШАЕТ ПРОБЛЕМУ С set_wakeup_fd
            )
            
        except Exception as e:
            logger.error(f"Критическая ошибка в работе Telegram бота: {e}", exc_info=True)
            raise

    def stop(self):
        """Остановка бота"""
        try:
            if self.application and self.is_running:
                logger.info("Остановка Telegram-бота...")
                # В асинхронной версии это сложнее, просто меняем флаг
                self.is_running = False
        except Exception as e:
            logger.error(f"Ошибка при остановке Telegram бота: {e}")