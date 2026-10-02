# test_image_upload.py - сквозной дымовой тест загрузки картинок (веб → диск → описание → список)
#
# Проверяется на реальной PostgreSQL и Flask test_client (внешняя сеть НЕ нужна):
#   1) POST /api/documents/upload принимает .jpg/.png и кладёт файл в Database/user_<id>;
#   2) неподдерживаемое расширение по-прежнему отклоняется (в тексте ошибки есть картинки);
#   3) GET /api/documents показывает картинку с группой «🖼 Изображения»;
#   4) распознавание (rag_core._process_file с подменённым requests.post) создаёт <имя>.txt
#      рядом с картинкой, и /api/documents помечает этот txt как RAG-файл (скрыт по умолчанию),
#      а группа наследуется от картинки;
#   5) содержимое источника: в чате источником будет ИМЯ КАРТИНКИ, а не .txt;
#   6) Pillow: большая картинка ужимается до image_max_side перед отправкой в API.
#
# Запуск из корня проекта:  venv/Scripts/python.exe tests/test_image_upload.py
import io
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import auth_db
import rag_core
import web_app

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  -> {extra!r}' if not cond and extra != '' else ''))


TEST_USER = '__img_upload_test__'
TEST_PASS = 'test1234'

ANSWER = ('ТЕКСТ:\n'
          'АКВАСЕГМЕНТ · Клапан обратный d32, арт. 4512\n'
          'ОПИСАНИЕ:\n'
          'Фото обратного клапана: серый корпус, шильдик с артикулом.')


class _Resp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {}, ensure_ascii=False)

    def json(self):
        return self._body


class _Post:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


class _Settings:
    """Настройки для распознавания: без БД, ключ-заглушка, свой базовый URL."""

    def __init__(self, **over):
        self.data = {'llm_api_key': 'test-key', 'image_model': 'qwen-vl-plus',
                     'image_base_url': 'https://example.invalid/compatible-mode/v1',
                     'image_max_side': 1600, 'image_enabled': True}
        self.data.update(over)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return self.data.get('llm_api_key', '')


def _jpeg(width=2400, height=1200):
    """Настоящая JPEG-картинка (Pillow). Без Pillow тест пропускает проверки ужатия."""
    from PIL import Image
    img = Image.new('RGB', (width, height), (245, 245, 245))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=80)
    return buf.getvalue()


def _register_user():
    ok, msg = auth_db.register_user(TEST_USER, TEST_PASS)
    if not ok and 'существует' not in str(msg).lower() and 'exist' not in str(msg).lower():
        raise RuntimeError(f'не удалось создать тестового пользователя: {msg}')
    rows = auth_db.db_rows if hasattr(auth_db, 'db_rows') else None
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s", (TEST_USER,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise RuntimeError('тестовый пользователь не найден в БД')
    return row[0]


def _cleanup(user_id):
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_documents WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()
    folder = os.path.join('Database', f'user_{user_id}')
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    auth_db.delete_user(user_id)


def main():
    user_id = _register_user()
    print(f"Тестовый пользователь id={user_id}")

    # Внешний мир и индексацию отключаем: проверяем загрузку и разметку списка
    web_app.rag_ready = False
    web_app._reindex_user_async = lambda uid: None
    logging.getLogger('rag_core').setLevel(logging.ERROR)

    client = web_app.app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = TEST_USER

    folder = os.path.join('Database', f'user_{user_id}')
    jpeg = _jpeg()

    # 1) загрузка картинок
    print('\n1) POST /api/documents/upload — картинки')
    data = {'files': [(io.BytesIO(jpeg), 'test_foto.jpg'), (io.BytesIO(jpeg), 'схема.png')],
            'is_price_list': '0'}
    r = client.post('/api/documents/upload', data=data, content_type='multipart/form-data')
    body = r.get_json() or {}
    check('HTTP 200', r.status_code == 200, r.status_code)
    check('успех в ответе', body.get('success') is True, body)
    check('файлы на диске', all(os.path.exists(os.path.join(folder, n))
                                for n in ('test_foto.jpg', 'схема.png')),
          os.listdir(folder) if os.path.isdir(folder) else 'нет папки')

    # 2) посторонний формат отклоняем
    print('\n2) Посторонний формат')
    r = client.post('/api/documents/upload', data={'files': [(io.BytesIO(b'MZ'), 'virus.exe')]},
                    content_type='multipart/form-data')
    err = (r.get_json() or {}).get('error', '')
    check('HTTP 400', r.status_code == 400, r.status_code)
    check('в тексте ошибки перечислены картинки', '.jpg' in err and '.png' in err, err)

    # 3) список документов: картинки в своей группе
    print('\n3) GET /api/documents — группа картинки')
    docs = {d['original_name']: d for d in (client.get('/api/documents').get_json() or {}).get('documents', [])}
    check('картинка в списке', 'test_foto.jpg' in docs, list(docs))
    check('группа «🖼 Изображения»', docs.get('test_foto.jpg', {}).get('doc_group') == '🖼 Изображения',
          docs.get('test_foto.jpg', {}).get('doc_group'))
    check('картинки не редактируются как txt', docs.get('test_foto.jpg', {}).get('can_edit') is False)

    # 4) распознавание: создаётся <имя>.txt
    print('\n4) Распознавание (подменённый API) → <имя>.txt')
    post = _Post([_Resp(200, {'choices': [{'message': {'content': ANSWER}}]})])
    rag_core.requests.post = post
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.settings = _Settings()
    rag.current_user_id = user_id
    knowledge = {}
    rag._process_file(os.path.join(folder, 'test_foto.jpg'), knowledge)
    txt_path = os.path.join(folder, 'test_foto.txt')
    check('API вызван один раз', post.calls == 1, post.calls)
    check('создан test_foto.txt', os.path.exists(txt_path))
    body_txt = open(txt_path, encoding='utf-8').read() if os.path.exists(txt_path) else ''
    check('в описании есть текст с картинки', 'Клапан обратный d32' in body_txt, body_txt[:200])
    check('в описании есть строка «что изображено»',
          'На изображении test_foto.jpg изображено:' in body_txt, body_txt[:200])
    check('источник в чате — имя картинки',
          rag._get_display_source_name(txt_path) == 'test_foto.jpg',
          rag._get_display_source_name(txt_path))

    # 5) список документов: txt помечен RAG-файлом и унаследовал группу картинки
    print('\n5) GET /api/documents — txt как RAG-файл')
    docs = {d['original_name']: d for d in (client.get('/api/documents').get_json() or {}).get('documents', [])}
    twin = docs.get('test_foto.txt', {})
    check('txt появился в списке', bool(twin), list(docs))
    check('txt помечен RAG-файлом (скрыт по умолчанию)', twin.get('is_rag_helper') is True, twin)
    check('txt наследует группу картинки', twin.get('doc_group') == '🖼 Изображения', twin.get('doc_group'))

    # 6) Pillow: картинка ужимается до image_max_side
    print('\n6) Ужатие картинки перед отправкой (Pillow)')
    try:
        from PIL import Image
        import base64
        data_url = rag_core.RAGCore._image_data_url(os.path.join(folder, 'test_foto.jpg'), 1600)
        head, b64 = data_url.split(',', 1)
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        check('отправляется JPEG', head.endswith('image/jpeg;base64'), head)
        check('сторона ужата до 1600 px', max(img.size) == 1600, img.size)
    except ImportError:
        print('  [SKIP] Pillow не установлен — проверка ужатия пропущена')

    _cleanup(user_id)
    print(f"\nИтог: OK {len(PASSED)}, FAIL {len(FAILED)}")
    if FAILED:
        print('Провалено:')
        for name in FAILED:
            print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
