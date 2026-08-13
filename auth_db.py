# auth_db.py - Модуль аутентификации пользователей через PostgreSQL
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
import logging
import json
import re

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
        # Миграция: колонка персонального промта пользователя (пусто = системный промт по умолчанию)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS base_prompt TEXT")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица users инициализирована")

        # Инициализация таблицы документов пользователей
        init_user_documents()

        # Инициализация таблицы прайс-листов
        init_price_items()

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


def get_all_users():
    """Получение списка всех зарегистрированных пользователей"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, username, created_at FROM users ORDER BY id")
        users = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(u) for u in users]
    except Exception as e:
        logger.error(f"Ошибка получения списка пользователей: {e}")
        return []


def get_user_prompt(user_id):
    """Получение персонального промта пользователя (None/'' = системный промт по умолчанию)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT base_prompt FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return row["base_prompt"] or None
    except Exception as e:
        logger.error(f"Ошибка получения промта пользователя: {e}")
        return None


def set_user_prompt(user_id, prompt):
    """Сохранение персонального промта пользователя (None/'' = сброс к системному промту)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET base_prompt = %s WHERE id = %s",
            (prompt or None, user_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения промта пользователя: {e}")
        return False


# === Управление документами пользователей ===

def init_user_documents():
    """Создание таблицы документов пользователей"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_documents (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename VARCHAR(255) NOT NULL,
                original_name VARCHAR(255) NOT NULL,
                file_hash VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_documents_user
            ON user_documents (user_id)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица user_documents инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации user_documents: {e}")
        return False


def add_document(user_id, filename, original_name, file_hash=None):
    """Добавление записи о документе пользователя"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_documents (user_id, filename, original_name, file_hash) VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, filename, original_name, file_hash),
        )
        doc_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Документ {original_name} добавлен пользователю {user_id}")
        return True, doc_id
    except Exception as e:
        logger.error(f"Ошибка добавления документа: {e}")
        return False, None


def delete_document(doc_id, user_id):
    """Удаление записи о документе (только для своего user_id)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_documents WHERE id = %s AND user_id = %s",
            (doc_id, user_id),
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted > 0
    except Exception as e:
        logger.error(f"Ошибка удаления документа: {e}")
        return False


def get_user_documents(user_id):
    """Получение списка документов пользователя"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, filename, original_name, created_at FROM user_documents WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        docs = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(d) for d in docs]
    except Exception as e:
        logger.error(f"Ошибка получения документов: {e}")
        return []


# === Прайс-листы ===

def init_price_items():
    """Создание таблицы позиций прайс-листов"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_items (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename VARCHAR(255) NOT NULL,
                article VARCHAR(255) DEFAULT '',
                name TEXT NOT NULL,
                price NUMERIC(14, 2),
                unit VARCHAR(50) DEFAULT '',
                raw TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_price_items_user ON price_items (user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_price_items_file ON price_items (user_id, filename)")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица price_items инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации price_items: {e}")
        return False


def replace_price_items(user_id, filename, rows):
    """Полная замена позиций прайс-листа для файла (повторная загрузка = свежий прайс).
    rows: список dict {article, name, price, unit, raw}"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM price_items WHERE user_id = %s AND filename = %s",
            (user_id, filename),
        )
        cur.executemany(
            "INSERT INTO price_items (user_id, filename, article, name, price, unit, raw) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    user_id,
                    filename,
                    row.get("article") or "",
                    row.get("name") or "",
                    row.get("price"),
                    row.get("unit") or "",
                    json.dumps(row.get("raw", {}), ensure_ascii=False),
                )
                for row in rows
            ],
        )
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"📋 Прайс-лист {filename}: заменено {len(rows)} позиций (user {user_id})")
        return True
    except Exception as e:
        logger.error(f"Ошибка замены прайс-листа {filename}: {e}")
        return False


def get_price_files(user_id):
    """Словарь filename → число позиций для всех прайсов пользователя"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT filename, COUNT(*) AS cnt FROM price_items WHERE user_id = %s GROUP BY filename",
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {r["filename"]: r["cnt"] for r in rows}
    except Exception as e:
        logger.error(f"Ошибка получения прайсов: {e}")
        return {}


def delete_price_items_for_file(user_id, filename):
    """Удаление всех позиций прайса при удалении документа"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM price_items WHERE user_id = %s AND filename = %s",
            (user_id, filename),
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted:
            logger.info(f"🗑️ Удалено {deleted} позиций прайса {filename} (user {user_id})")
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления прайса {filename}: {e}")
        return False


# Стоп-слова ценового запроса: вырезаются перед поиском по артикулу/наименованию
_PRICE_STOP_WORDS = (
    "сколько стоит", "сколько", "цена", "цены", "цену", "стоит", "стоят", "стоимость",
    "на", "за", "по", "артикул", "артикулу", "арт", "прайс", "прайсу", "прайс-лист",
    "рублей", "рубля", "рубль", "руб", "р.", "руб.", "₽", "скажи", "подскажи",
    "мне", "пожалуйста", "товар", "товара", "позиция", "какой", "какая", "какие",
)


def _normalize_price_query(query):
    """Убирает стоп-слова, пунктуацию и лишние пробелы из ценового запроса.
    Сохраняет кириллицу, цифры, дефисы (артикулы вида ТЧ-2000) и × (размеры 2000×2000)."""
    q = query.lower()
    for word in _PRICE_STOP_WORDS:
        q = q.replace(word, " ")
    # Пунктуация → пробел (кроме дефиса внутри артикула и × в размерах)
    q = re.sub(r"[^\w\s×хx\-]", " ", q, flags=re.UNICODE)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def search_price_items(user_id, query, limit=5):
    """Точный поиск по прайс-листу: артикул → полное совпадение фразы в наименовании →
    все значимые слова наименования. Возвращает список dict {id, filename, article, name, price, unit}"""
    try:
        clean = _normalize_price_query(query)
        if not clean:
            return []
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        like = f"%{clean}%"
        # 1) По артикулу (точное совпадение — первым)
        cur.execute(
            "SELECT id, filename, article, name, price, unit FROM price_items "
            "WHERE user_id = %s AND article <> '' AND article ILIKE %s "
            "ORDER BY (article ILIKE %s) DESC, name LIMIT %s",
            (user_id, like, clean, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        # 2) Полная фраза в наименовании
        if not rows:
            cur.execute(
                "SELECT id, filename, article, name, price, unit FROM price_items "
                "WHERE user_id = %s AND name ILIKE %s "
                "ORDER BY (name ILIKE %s) DESC, length(name) LIMIT %s",
                (user_id, like, clean, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        # 3) Все значимые слова запроса (AND по токенам ≥3 символов)
        if not rows and len(clean) > 3:
            tokens = [t for t in re.split(r"\s+", clean) if len(t) >= 3]
            if tokens:
                cond = " AND ".join(["name ILIKE %s"] * len(tokens))
                params = [user_id] + [f"%{t}%" for t in tokens] + [limit]
                cur.execute(
                    f"SELECT id, filename, article, name, price, unit FROM price_items "
                    f"WHERE user_id = %s AND ({cond}) ORDER BY length(name) LIMIT %s",
                    params,
                )
                rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Ошибка поиска по прайсу: {e}")
        return []


# === История чата ===

def init_chat_history():
    """Создание таблицы истории чата"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role VARCHAR(10) NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Индекс для быстрой загрузки истории по пользователю
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_history_user
            ON chat_history (user_id, created_at)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица chat_history инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации chat_history: {e}")
        return False


def save_message(user_id, role, message):
    """Сохранение сообщения в историю чата, возвращает id сообщения"""
    if not user_id or not message:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (user_id, role, message) VALUES (%s, %s, %s) RETURNING id",
            (user_id, role, message),
        )
        msg_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return msg_id
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения: {e}")
        return None


def get_history(user_id, limit=50):
    """Загрузка истории чата пользователя"""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, role, message, created_at FROM chat_history "
            "WHERE user_id = %s ORDER BY created_at ASC LIMIT %s",
            (user_id, limit),
        )
        messages = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(m) for m in messages]
    except Exception as e:
        logger.error(f"Ошибка загрузки истории: {e}")
        return []


def clear_chat_history(user_id):
    """Очистка истории чата пользователя"""
    if not user_id:
        return False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_history WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"🗑️ История чата пользователя #{user_id} очищена (удалено {deleted} записей)")
        return True
    except Exception as e:
        logger.error(f"Ошибка очистки истории чата: {e}")
        return False


def delete_message(message_id, user_id):
    """Удаление одного сообщения из истории чата"""
    if not message_id or not user_id:
        return False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM chat_history WHERE id = %s AND user_id = %s",
            (message_id, user_id),
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted > 0:
            logger.info(f"🗑️ Удалено сообщение #{message_id} пользователя #{user_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления сообщения: {e}")
        return False


def delete_message_pair(assistant_id, user_id):
    """Удаление пары сообщений: ответ бота и предыдущий вопрос пользователя"""
    if not assistant_id or not user_id:
        return False
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Получаем сообщение бота
        cur.execute(
            "SELECT id, role, created_at FROM chat_history WHERE id = %s AND user_id = %s",
            (assistant_id, user_id),
        )
        assistant_msg = cur.fetchone()
        if not assistant_msg or assistant_msg['role'] != 'assistant':
            cur.close()
            conn.close()
            return False

        # Ищем предыдущее сообщение пользователя
        cur.execute(
            "SELECT id FROM chat_history WHERE user_id = %s AND role = 'user' "
            "AND created_at < %s ORDER BY created_at DESC LIMIT 1",
            (user_id, assistant_msg['created_at']),
        )
        user_msg = cur.fetchone()

        # Удаляем оба сообщения
        ids_to_delete = [assistant_id]
        if user_msg:
            ids_to_delete.append(user_msg['id'])

        cur.execute(
            "DELETE FROM chat_history WHERE id = ANY(%s) AND user_id = %s",
            (ids_to_delete, user_id),
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"🗑️ Удалена пара сообщений #{ids_to_delete} пользователя #{user_id} (удалено {deleted})")
        return True, ids_to_delete
    except Exception as e:
        logger.error(f"Ошибка удаления пары сообщений: {e}")
        return False, []