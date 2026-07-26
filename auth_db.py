# auth_db.py - Модуль аутентификации пользователей через PostgreSQL
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
import logging

logger = logging.getLogger(__name__)

# Настройки подключения к PostgreSQL для БД rag_system
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "rag_system",
    "user": "postgres",
    "password": "nfhpfyktktdf12",
}


def get_db_connection():
    """Создание подключения к PostgreSQL"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")
        raise


def init_db():
    """Инициализация базы данных: создание таблицы пользователей"""
    try:
        # Сначала подключаемся к БД postgres, чтобы создать rag_system если её нет
        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            dbname="postgres",
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (DB_CONFIG["dbname"],),
        )
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{DB_CONFIG["dbname"]}"')
            logger.info(f"База данных {DB_CONFIG['dbname']} создана")
        cur.close()
        conn.close()

        # Теперь подключаемся к rag_system и создаём таблицу users
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица users инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False


def register_user(username, password):
    """Регистрация нового пользователя"""
    if not username or not password:
        return False, "Имя пользователя и пароль обязательны"

    if len(username) < 3:
        return False, "Имя пользователя должно быть не менее 3 символов"

    if len(password) < 4:
        return False, "Пароль должен быть не менее 4 символов"

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Проверяем, существует ли пользователь
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return False, "Пользователь с таким именем уже существует"

        # Хэшируем пароль и создаём пользователя
        password_hash = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
            (username, password_hash),
        )
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Пользователь {username} зарегистрирован")
        return True, "Регистрация успешна"
    except Exception as e:
        logger.error(f"Ошибка регистрации: {e}")
        return False, f"Ошибка регистрации: {str(e)}"


def login_user(username, password):
    """Проверка логина и пароля пользователя"""
    if not username or not password:
        return False, "Имя пользователя и пароль обязательны"

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            logger.info(f"Пользователь {username} успешно вошёл")
            return True, user
        else:
            return False, "Неверное имя пользователя или пароль"
    except Exception as e:
        logger.error(f"Ошибка входа: {e}")
        return False, f"Ошибка входа: {str(e)}"