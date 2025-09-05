# telegram_bot.py - Telegram бот для RAG-системы
import asyncio
import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from rag_core import get_rag_system
import os

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен вашего бота (указывайте реальный токен вместо примера)
BOT_TOKEN = "7502394795:AAFlLzJzlI-ciWxmSQMF6poO4uWZx-p-bK0"

# ID администраторов (добавьте сюда ID пользователей, которые могут использовать команды администратора)
ADMIN_USER_IDS = set()  # Пример: {123456789, 987654321}

class TelegramRAGBot:
    def __init__(self):
        """Инициализация Telegram-бота"""
        self.rag_system = get_rag_system()
        self.application = None
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        welcome_message = (
            f"🌟 Привет, {user.first_name}! Добро пожаловать в RAG-бот компании Аквесегмент!\n\n"
            f"🤖 Я интеллектуальный помощник, созданный на базе искусственного интеллекта.\n"
            f"💬 Просто задайте мне вопрос, и я постараюсь на него ответить, используя нашу базу знаний.\n\n"
            f"❓ Примеры вопросов:\n"
            f"• Какие услуги вы предоставляете?\n"
            f"• Кто является ответственным за проект X?\n"
            f"• Как связаться с отделом продаж?\n\n"
            f"Команды:\n"
            f"/start - Начальное сообщение\n"
            f"/help - Помощь\n"
            f"/stats - Статистика (только для администраторов)\n"
            f"/reload - Перезагрузка базы знаний (только для администраторов)"
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
            "/stats - Статистика (для администраторов)\n"
            "/reload - Перезагрузка базы знаний (для администраторов)\n\n"
            "⚠ *Важно:*\n"
            "• Ответы формируются на основе имеющейся базы знаний\n"
            "• Если ответа нет в базе, бот сообщит об этом\n"
            "• Для уточнения вопросов переформулируйте их более конкретно"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')
        logger.info(f"Пользователь {update.effective_user.first_name} запросил помощь")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stats (только для администраторов)"""
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        
        if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
            await update.message.reply_text("❌ У вас нет прав для просмотра статистики.")
            logger.warning(f"Пользователь {user_name} (ID: {user_id}) попытался получить статистику без прав")
            return
            
        # Здесь можно добавить реальную статистику
        stats_text = (
            "📊 *Статистика системы:*\n\n"
            f"📚 Фрагментов в базе знаний: {len(self.rag_system.my_knowledge)}\n"
            f"📁 Загружено файлов: {len(self.rag_system.all_knowledge_dict)}\n"
            f"🧠 Модель поиска: SentenceTransformer\n"
            f"🔤 Методы поиска: Гибридный (Semantic + BM25)\n"
            f"🤖 Активные модели ИИ: {len(self.rag_system.__class__.__bases__[0].__dict__.get('AVAILABLE_MODELS', [])) if hasattr(self.rag_system, 'AVAILABLE_MODELS') else 'N/A'}"
        )
        await update.message.reply_text(stats_text, parse_mode='Markdown')
        logger.info(f"Администратор {user_name} (ID: {user_id}) запросил статистику")

    async def reload_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /reload (только для администраторов)"""
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        
        if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
            await update.message.reply_text("❌ У вас нет прав для перезагрузки базы знаний.")
            logger.warning(f"Пользователь {user_name} (ID: {user_id}) попытался перезагрузить базу знаний без прав")
            return
            
        try:
            await update.message.reply_text("🔄 Перезагрузка базы знаний. Пожалуйста, подождите...")
            logger.info(f"Администратор {user_name} (ID: {user_id}) инициировал перезагрузку базы знаний")
            
            # Выполняем перезагрузку в отдельной задаче, чтобы не блокировать Telegram
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, self.rag_system.reload_knowledge_base)
            
            if success:
                reload_message = (
                    "✅ База знаний успешно перезагружена!\n\n"
                    f"📚 Загружено фрагментов: {len(self.rag_system.my_knowledge)}\n"
                    f"📁 Из файлов: {len(self.rag_system.all_knowledge_dict)}"
                )
                await update.message.reply_text(reload_message)
                logger.info(f"База знаний успешно перезагружена администратором {user_name}")
            else:
                await update.message.reply_text("❌ Ошибка при перезагрузке базы знаний. Проверьте логи.")
                logger.error(f"Ошибка перезагрузки базы знаний администратором {user_name}")
                
        except Exception as e:
            error_message = f"❌ Произошла ошибка при перезагрузке базы знаний: {str(e)}"
            await update.message.reply_text(error_message)
            logger.error(f"Ошибка при перезагрузке базы знаний администратором {user_name}: {e}")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user_message = update.message.text
        user = update.effective_user
        
        logger.info(f"Получено сообщение от {user.first_name} (@{user.username}, ID: {user.id}): {user_message}")
        
        try:
            # Уведомление о том, что сообщение обрабатывается
            await update.message.chat.send_action("typing")
            
            # Обработка запроса через RAG-систему
            # Выполняем в отдельном потоке, чтобы не блокировать Telegram
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, self.rag_system.ask_model, user_message)
            
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
                "2. Попробовать позже\n"
                "3. Обратиться к администратору бота"
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
            BotCommand("stats", "Статистика (для администраторов)"),
            BotCommand("reload", "Перезагрузка базы знаний (для администраторов)")
        ])
        logger.info("Команды бота установлены")

    def run(self):
        """Запуск бота"""
        # Создание приложения
        self.application = Application.builder().token(BOT_TOKEN).post_init(self.post_init).build()

        # Регистрация обработчиков
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("reload", self.reload_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

        # Запуск бота
        logger.info("Telegram-бот запущен. Ожидание сообщений...")
        self.application.run_polling()

# Запуск бота
if __name__ == '__main__':
    # Удалите или закомментируйте этот блок:
    # if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
    #     logger.error("❌ Не установлен токен Telegram-бота...")
    #     exit(1)
    
    # Создание и запуск бота
    try:
        bot = TelegramRAGBot()
        bot.run()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске бота: {e}", exc_info=True)
        print(f"\n❌ Критическая ошибка при запуске бота: {e}")
        exit(1)