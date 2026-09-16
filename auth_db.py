# auth_db.py - Модуль аутентификации пользователей через PostgreSQL
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
import logging
import json
import re
import os
import secrets

logger = logging.getLogger(__name__)

# Параметры подключения к PostgreSQL. Пароль и отличия платформ НЕ хранятся в коде
# (чтобы не попадать в git), а берутся из db_config.json (gitignored) или переменных
# окружения. Несекретные значения по умолчанию оставлены здесь для удобства.
_DB_DEFAULTS = {
    "host": "localhost",
    "port": 5432,
    "dbname": "rag_system",
    "user": "postgres",
    "password": "",
}

# Файл с параметрами подключения к БД (gitignored; содержит ТОЛЬКО db_* —
# все остальные настройки хранятся в таблице app_settings).
_DB_CONFIG_FILE = "db_config.json"

# Устаревший файл настроек: используется ТОЛЬКО для одноразовой миграции
# (см. _ensure_db_config_file / init_app_settings) — приложение его не читает.
_LEGACY_SETTINGS_FILE = "rag_settings.json"

# Ключи db_config.json -> параметры psycopg2
_DB_SETTINGS_KEYS = {
    "db_host": "host",
    "db_port": "port",
    "db_name": "dbname",
    "db_user": "user",
    "db_password": "password",
}

# Переменные окружения -> параметры psycopg2 (приоритет над файлом)
_DB_ENV_KEYS = {
    "PGHOST": "host",
    "PGPORT": "port",
    "PGDATABASE": "dbname",
    "PGUSER": "user",
    "PGPASSWORD": "password",
}


def _load_legacy_settings():
    """Чтение устаревшего rag_settings.json (только для одноразовой миграции)."""
    try:
        with open(_LEGACY_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ensure_db_config_file():
    """Одноразовая миграция: если db_config.json отсутствует, а в устаревшем
    rag_settings.json есть db_* — создать db_config.json из них (бесшовный переход
    на сервере после git pull; локально файл уже создан)."""
    if os.path.exists(_DB_CONFIG_FILE):
        return
    legacy = _load_legacy_settings()
    db_cfg = {skey: v for skey, v in legacy.items() if skey in _DB_SETTINGS_KEYS and v not in (None, "")}
    if not db_cfg:
        return
    try:
        tmp = _DB_CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db_cfg, f, indent=4, ensure_ascii=False)
        os.replace(tmp, _DB_CONFIG_FILE)
        logger.warning("⚠️ db_config.json создан из устаревшего rag_settings.json (ключи db_*)")
    except Exception as e:
        logger.error(f"Не удалось создать db_config.json: {e}")


def _get_db_config():
    """Эффективные параметры подключения: defaults <- db_config.json
    <- (устаревший rag_settings.json, только если db_config.json нет) <- env."""
    cfg = dict(_DB_DEFAULTS)
    config_file = _DB_CONFIG_FILE if os.path.exists(_DB_CONFIG_FILE) else _LEGACY_SETTINGS_FILE
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            s = json.load(f)
        for skey, ckey in _DB_SETTINGS_KEYS.items():
            v = s.get(skey)
            if v not in (None, ""):
                cfg[ckey] = v
    except Exception:
        pass  # файла нет или битый — остаются defaults/env
    for ekey, ckey in _DB_ENV_KEYS.items():
        v = os.environ.get(ekey)
        if v:
            cfg[ckey] = v
    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        cfg["port"] = 5432
    return cfg


def get_db_connection():
    """Создание подключения к PostgreSQL"""
    try:
        conn = psycopg2.connect(**_get_db_config())
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")
        raise


def init_db():
    """Инициализация базы данных: создание таблицы пользователей"""
    try:
        # Одноразовая миграция: db_config.json из устаревшего rag_settings.json (если нет)
        _ensure_db_config_file()

        # Сначала подключаемся к БД postgres, чтобы создать rag_system если её нет
        dbcfg = _get_db_config()
        conn = psycopg2.connect(
            host=dbcfg["host"],
            port=dbcfg["port"],
            dbname="postgres",
            user=dbcfg["user"],
            password=dbcfg["password"],
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (dbcfg["dbname"],),
        )
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{dbcfg["dbname"]}"')
            logger.info(f"База данных {dbcfg['dbname']} создана")
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

        # Инициализация таблицы настроек (app_settings) + одноразовый перенос из rag_settings.json
        init_app_settings()

        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False


def init_app_settings():
    """Таблица app_settings (ключ-значение; значения JSON-сериализованы).
    Если таблица пуста и есть устаревший rag_settings.json — одноразовый перенос
    всех настроек (кроме db_*) в БД. Вызывается из init_db()."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("SELECT COUNT(*) FROM app_settings")
        if cur.fetchone()[0] == 0:
            legacy = {k: v for k, v in _load_legacy_settings().items() if k not in _DB_SETTINGS_KEYS}
            if legacy:
                for key, value in legacy.items():
                    cur.execute(
                        "INSERT INTO app_settings (key, value) VALUES (%s, %s)",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
                logger.warning(
                    f"⚙️ Настройки перенесены из rag_settings.json в app_settings ({len(legacy)} ключей)"
                )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации app_settings: {e}")
        return False


def get_all_settings():
    """Все настройки из app_settings (значения JSON-десериализованы).
    При ошибке БД возвращает {} — вызывающий код подставляет defaults."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_settings")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = {}
        for key, raw in rows:
            try:
                result[key] = json.loads(raw)
            except Exception:
                result[key] = raw  # не JSON — храним как строку
        return result
    except Exception as e:
        logger.error(f"Ошибка чтения настроек из БД: {e}")
        return {}


def set_settings(mapping):
    """Сохранение набора настроек в app_settings (UPSERT по ключам)."""
    if not mapping:
        return True
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for key, value in mapping.items():
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек в БД: {e}")
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
                doc_group VARCHAR(100) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Миграция: doc_group для уже существующих таблиц (CREATE IF NOT EXISTS не добавит колонку)
        cur.execute("ALTER TABLE user_documents ADD COLUMN IF NOT EXISTS doc_group VARCHAR(100) DEFAULT ''")
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


def add_document(user_id, filename, original_name, file_hash=None, doc_group=''):
    """Добавление записи о документе пользователя"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_documents (user_id, filename, original_name, file_hash, doc_group) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, filename, original_name, file_hash, doc_group),
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
            "SELECT id, filename, original_name, doc_group, created_at FROM user_documents WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        docs = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(d) for d in docs]
    except Exception as e:
        logger.error(f"Ошибка получения документов: {e}")
        return []


def update_document_group(doc_id, user_id, doc_group):
    """Обновление группы документа (для ленивого бэкфилла старых файлов)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_documents SET doc_group = %s WHERE id = %s AND user_id = %s",
            (doc_group, doc_id, user_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления группы документа: {e}")
        return False


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
        # Миграция: колонка устройства — у каждого устройства (браузера) свой чат.
        # Старые записи получают метку 'web' и показываются, пока нет своей истории.
        cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS device_id VARCHAR(64) DEFAULT 'web'")
        # Индекс для быстрой загрузки истории по пользователю
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_history_user
            ON chat_history (user_id, created_at)
        """)
        # Индекс для быстрой загрузки истории по пользователю и устройству
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_history_device
            ON chat_history (user_id, device_id, created_at)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица chat_history инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации chat_history: {e}")
        return False

def init_query_analytics():
    """Создание таблицы аналитики запросов (кнопка «Удалить весь чат» её не очищает)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS query_analytics (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                answer TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Индекс для быстрой выборки аналитики по пользователю
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_query_analytics_user
            ON query_analytics (user_id, created_at)
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблица query_analytics инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации query_analytics: {e}")
        return False



def save_message(user_id, role, message, device_id='web'):
    """Сохранение сообщения в историю чата, возвращает id сообщения.
    device_id — идентификатор устройства (браузера); у каждого устройства свой чат."""
    if not user_id or not message:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (user_id, role, message, device_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, role, message, device_id),
        )
        msg_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return msg_id
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения: {e}")
        return None


def save_query_analytics(user_id, question, answer=None):
    """Сохранение пары вопрос-ответ в аналитику (не удаляется при очистке чата)"""
    if not user_id or not question:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO query_analytics (user_id, question, answer) VALUES (%s, %s, %s)",
            (user_id, question, answer),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения аналитики запроса: {e}")
        return None


def get_history(user_id, limit=50, device_id='web'):
    """Загрузка истории чата пользователя для конкретного устройства.

    Пока у пользователя нет записей со своих устройств (device_id != 'web'),
    показывается «наследственная» история (device_id='web') — данные не теряются.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, role, message, created_at FROM chat_history "
            "WHERE user_id = %s AND (device_id = %s OR (device_id = 'web' AND NOT EXISTS ("
            "    SELECT 1 FROM chat_history c2 WHERE c2.user_id = %s AND c2.device_id <> 'web'"
            "))) ORDER BY created_at ASC LIMIT %s",
            (user_id, device_id, user_id, limit),
        )
        messages = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(m) for m in messages]
    except Exception as e:
        logger.error(f"Ошибка загрузки истории: {e}")
        return []


def clear_chat_history(user_id, device_id=None):
    """Очистка истории чата: для конкретного устройства (device_id) или всей, если не задан"""
    if not user_id:
        return False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "DELETE FROM chat_history WHERE user_id = %s AND device_id = %s",
                (user_id, device_id),
            )
            log_note = f"устройства {device_id}"
        else:
            cur.execute("DELETE FROM chat_history WHERE user_id = %s", (user_id,))
            log_note = "всех устройств"
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"🗑️ История чата пользователя #{user_id} очищена ({log_note}, удалено {deleted} записей)")
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

def get_analytics(user_id):
    """Аналитика запросов текущего пользователя (только свои данные)"""
    empty = {
        "total": 0,
        "top_questions": [],
        "activity": [],
        "unanswered": [],
        "recent": [],
    }
    if not user_id:
        return empty
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT COUNT(*) AS c FROM query_analytics WHERE user_id = %s", (user_id,))
        total = cur.fetchone()["c"]

        cur.execute(
            "SELECT question, COUNT(*) AS c FROM query_analytics "
            "WHERE user_id = %s GROUP BY question ORDER BY c DESC LIMIT 10",
            (user_id,),
        )
        top_questions = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM query_analytics "
            "WHERE user_id = %s GROUP BY DATE(created_at) ORDER BY d DESC LIMIT 30",
            (user_id,),
        )
        activity = [dict(r) for r in cur.fetchall()]
        activity.reverse()

        cur.execute(
            "SELECT question, created_at FROM query_analytics "
            "WHERE user_id = %s AND (answer IS NULL OR BTRIM(answer) = '') "
            "ORDER BY created_at DESC LIMIT 20",
            (user_id,),
        )
        unanswered = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT question, answer, created_at FROM query_analytics "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        )
        recent = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()
        return {
            "total": total,
            "top_questions": top_questions,
            "activity": activity,
            "unanswered": unanswered,
            "recent": recent,
        }
    except Exception as e:
        logger.error(f"Ошибка получения аналитики: {e}")
        return empty
# === Админ-панель: удаление пользователей и аналитики ===

def delete_user(user_id):
    """Полное удаление пользователя из БД.

    Все дочерние таблицы (user_documents, price_items, chat_history,
    query_analytics) имеют ON DELETE CASCADE — каскад чистит их автоматически.
    """
    if not user_id:
        return False, 0
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted:
            logger.info(f"🗑️ Пользователь #{user_id} удалён из БД (каскад: документы/прайсы/чат/аналитика)")
        return deleted > 0, deleted
    except Exception as e:
        logger.error(f"Ошибка удаления пользователя #{user_id}: {e}")
        return False, 0


def delete_user_analytics(user_id):
    """Удаление аналитики запросов пользователя (только query_analytics)."""
    if not user_id:
        return 0
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM query_analytics WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"🗑️ Аналитика пользователя #{user_id}: удалено {deleted} записей")
        return deleted
    except Exception as e:
        logger.error(f"Ошибка удаления аналитики пользователя #{user_id}: {e}")
        return 0



# ============================================================
# Сайт-виджеты: чат-бот для встраивания на сайт клиента
# ============================================================

def init_widgets():
    """Таблицы виджетов: настройки (widgets) + счётчик дневных обращений (widget_hits)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS widgets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                key VARCHAR(64) UNIQUE NOT NULL,
                allowed_domains TEXT DEFAULT '',
                theme_color VARCHAR(9) DEFAULT '#667eea',
                daily_limit INTEGER DEFAULT 200,
                active BOOLEAN DEFAULT TRUE,
                total_requests INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP
            )
        """)
        # Счётчик обращений по дням: дневной лимит снимается атомарно (widget_consume)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS widget_hits (
                widget_id INTEGER NOT NULL REFERENCES widgets(id) ON DELETE CASCADE,
                day DATE NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (widget_id, day)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблицы виджетов инициализированы")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации widgets: {e}")
        return False


def create_widget(user_id, name, allowed_domains='', daily_limit=200, theme_color='#667eea'):
    """Создать виджет. Возвращает (ok, строка-виджет с ключом)."""
    try:
        key = secrets.token_urlsafe(16)
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "INSERT INTO widgets (user_id, name, key, allowed_domains, theme_color, daily_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (user_id, name, key, (allowed_domains or '').strip().lower(), theme_color, int(daily_limit)),
        )
        row = dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Создан виджет «{name}» (#{row['id']}) для пользователя {user_id}")
        return True, row
    except Exception as e:
        logger.error(f"Ошибка создания виджета: {e}")
        return False, None


def get_widget_by_key(key):
    """Виджет по публичному ключу."""
    if not key or len(key) > 64:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM widgets WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Ошибка получения виджета: {e}")
        return None


def list_user_widgets(user_id):
    """Все виджеты пользователя + счётчик обращений за сегодня."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT w.*, COALESCE(h.hits, 0) AS today_hits
            FROM widgets w
            LEFT JOIN widget_hits h ON h.widget_id = w.id AND h.day = CURRENT_DATE
            WHERE w.user_id = %s
            ORDER BY w.created_at DESC
        """, (user_id,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Ошибка списка виджетов: {e}")
        return []


_WIDGET_COLS = {'name', 'allowed_domains', 'daily_limit', 'theme_color', 'active'}


def update_widget(user_id, widget_id, fields):
    """Точечное обновление виджета (только своего и только белых колонок)."""
    fields = {k: v for k, v in fields.items() if k in _WIDGET_COLS}
    if not fields:
        return False
    try:
        sets = ', '.join(f"{k} = %s" for k in fields)
        vals = list(fields.values()) + [widget_id, user_id]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"UPDATE widgets SET {sets} WHERE id = %s AND user_id = %s", vals)
        changed = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return changed
    except Exception as e:
        logger.error(f"Ошибка обновления виджета: {e}")
        return False


def delete_widget(user_id, widget_id):
    """Удаление своего виджета (обращения каскадно удаляются)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM widgets WHERE id = %s AND user_id = %s", (widget_id, user_id))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as e:
        logger.error(f"Ошибка удаления виджета: {e}")
        return False


def widget_consume(widget_id):
    """Атомарно снять единицу дневного лимита. True — запрос разрешён, False — лимит исчерпан."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO widget_hits (widget_id, day, hits)
            VALUES (%s, CURRENT_DATE, 1)
            ON CONFLICT (widget_id, day) DO UPDATE
                SET hits = widget_hits.hits + 1
            WHERE widget_hits.hits < (SELECT daily_limit FROM widgets WHERE widgets.id = %s)
        """, (widget_id, widget_id))
        consumed = cur.rowcount > 0
        if consumed:
            cur.execute(
                "UPDATE widgets SET total_requests = total_requests + 1, last_used_at = CURRENT_TIMESTAMP WHERE id = %s",
                (widget_id,),
            )
        conn.commit()
        cur.close()
        conn.close()
        return consumed
    except Exception as e:
        logger.error(f"Ошибка учёта обращений виджета: {e}")
        return False


def get_widget_history(user_id, device_scope, limit=100):
    """История чата одного посетителя виджета (строго своя область, без fallback на 'web')."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, role, message, created_at FROM chat_history "
            "WHERE user_id = %s AND device_id = %s ORDER BY created_at ASC LIMIT %s",
            (user_id, device_scope, limit),
        )
        messages = [dict(m) for m in cur.fetchall()]
        cur.close()
        conn.close()
        return messages
    except Exception as e:
        logger.error(f"Ошибка загрузки истории виджета: {e}")
        return []


def clear_widget_history(user_id, device_scope):
    """Очистка истории чата одного посетителя виджета."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_history WHERE user_id = %s AND device_id = %s", (user_id, device_scope))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка очистки истории виджета: {e}")
        return False


def get_all_users_with_stats():
    """Список всех пользователей со счётчиками данных (для админ-панели)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.username, u.created_at,
                   (SELECT COUNT(*) FROM user_documents d WHERE d.user_id = u.id) AS docs,
                   (SELECT COUNT(*) FROM price_items p WHERE p.user_id = u.id) AS prices,
                   (SELECT COUNT(*) FROM chat_history c WHERE c.user_id = u.id) AS messages,
                   (SELECT COUNT(*) FROM query_analytics q WHERE q.user_id = u.id) AS analytics
            FROM users u
            ORDER BY u.id
        """)
        users = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return users
    except Exception as e:
        logger.error(f"Ошибка получения списка пользователей со счётчиками: {e}")
        return []


# === MAX-каналы (чат-бот в мессенджере MAX) ===
# Один бот на пользователя RAG: токен владелец вводит сам в кабинете, он лежит
# в БД (не в файлах проекта). Переписка собеседников — в общей chat_history
# с device_id='max:<channel_id>:<max_user_id>', поэтому аналитика и панель
# работают теми же функциями, что и для сайт-виджета.

def init_max_channels():
    """Таблицы канала MAX: подключение (токен, ключ вебхука) и счётчик обращений."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS max_channels (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                token VARCHAR(200) NOT NULL,
                bot_user_id BIGINT,
                bot_name VARCHAR(100) DEFAULT '',
                bot_username VARCHAR(100) DEFAULT '',
                hook_key VARCHAR(64) UNIQUE NOT NULL,
                hook_secret VARCHAR(64) NOT NULL,
                mode VARCHAR(16) DEFAULT 'webhook',
                daily_limit INTEGER DEFAULT 200,
                active BOOLEAN DEFAULT TRUE,
                total_requests INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP
            )
        """)
        # Счётчик ответов по дням: дневной лимит снимается атомарно (max_consume)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS max_hits (
                channel_id INTEGER NOT NULL REFERENCES max_channels(id) ON DELETE CASCADE,
                day DATE NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (channel_id, day)
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Таблицы канала MAX инициализированы")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации таблиц MAX: {e}")
        return False


def get_max_channel(user_id):
    """Подключение MAX пользователя + счётчик ответов за сегодня."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, COALESCE(h.hits, 0) AS today_hits
            FROM max_channels c
            LEFT JOIN max_hits h ON h.channel_id = c.id AND h.day = CURRENT_DATE
            WHERE c.user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Ошибка получения канала MAX: {e}")
        return None


def get_max_channel_by_hook(hook_key):
    """Канал по ключу вебхука (публичный адрес /max/hook/<ключ>)."""
    if not hook_key or len(hook_key) > 64:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM max_channels WHERE hook_key = %s", (hook_key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Ошибка получения канала MAX по ключу: {e}")
        return None


def list_active_max_channels():
    """Все включённые каналы (для супервизора long polling)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM max_channels WHERE active = TRUE")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Ошибка списка каналов MAX: {e}")
        return []


def save_max_channel(user_id, token, bot_user_id, bot_name, bot_username,
                     hook_key, hook_secret, daily_limit=200):
    """Создать или обновить подключение MAX (один бот на пользователя)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO max_channels
                (user_id, token, bot_user_id, bot_name, bot_username, hook_key, hook_secret, daily_limit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                token = EXCLUDED.token,
                bot_user_id = EXCLUDED.bot_user_id,
                bot_name = EXCLUDED.bot_name,
                bot_username = EXCLUDED.bot_username,
                daily_limit = EXCLUDED.daily_limit
            RETURNING *
        """, (user_id, token, bot_user_id, bot_name, bot_username, hook_key, hook_secret, int(daily_limit)))
        row = dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"MAX-канал сохранён для пользователя {user_id} (бот @{bot_username})")
        return True, row
    except Exception as e:
        logger.error(f"Ошибка сохранения канала MAX: {e}")
        return False, None


_MAX_COLS = {'daily_limit', 'active', 'mode', 'bot_name', 'bot_username', 'token'}


def update_max_channel(user_id, fields):
    """Точечное обновление своего канала MAX (только белые колонки)."""
    fields = {k: v for k, v in fields.items() if k in _MAX_COLS}
    if not fields:
        return False
    try:
        sets = ', '.join(f"{k} = %s" for k in fields)
        vals = list(fields.values()) + [user_id]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"UPDATE max_channels SET {sets} WHERE user_id = %s", vals)
        changed = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return changed
    except Exception as e:
        logger.error(f"Ошибка обновления канала MAX: {e}")
        return False


def delete_max_channel(user_id):
    """Удалить подключение MAX (счётчики обращений удаляются каскадом)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM max_channels WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as e:
        logger.error(f"Ошибка удаления канала MAX: {e}")
        return False


def max_consume(channel_id):
    """Атомарно снять единицу дневного лимита канала. False — лимит исчерпан."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO max_hits (channel_id, day, hits)
            VALUES (%s, CURRENT_DATE, 1)
            ON CONFLICT (channel_id, day) DO UPDATE
                SET hits = max_hits.hits + 1
            WHERE max_hits.hits < (SELECT daily_limit FROM max_channels WHERE max_channels.id = %s)
        """, (channel_id, channel_id))
        consumed = cur.rowcount > 0
        if consumed:
            cur.execute(
                "UPDATE max_channels SET total_requests = total_requests + 1, "
                "last_used_at = CURRENT_TIMESTAMP WHERE id = %s",
                (channel_id,),
            )
        conn.commit()
        cur.close()
        conn.close()
        return consumed
    except Exception as e:
        logger.error(f"Ошибка учёта обращений MAX: {e}")
        return False
