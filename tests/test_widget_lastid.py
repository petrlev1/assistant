# test_widget_lastid.py — виджет: сервер сообщает, до какого id лента показана гостю
#
# Что проверяется (жалоба: «в виджете диалоги дублируются, в Диалогах всё нормально»):
#   гость видит свой вопрос и ответ сразу (локальная отрисовка), а лента ещё и
#   опрашивается раз в 5 с с after_id=<последний показанный id>. Если
#   /api/widget/ask не говорит, что эти реплики уже записаны, опрос возвращает их
#   снова — каждая пара «вопрос-ответ» появляется в ленте дважды.
#   1) ответ несёт last_id = id последней записанной реплики;
#   2) опрос с after_id=last_id не отдаёт ничего (дубля нет), а с прежним after_id
#      отдаёт обе реплики — то есть до правки клиент показывал их второй раз;
#   3) ручной режим (ведёт менеджер): last_id = id вопроса гостя, опрос молчит;
#   4) исчерпанный дневной лимит: ничего не пишем → last_id = 0;
#   5) сбой модели: вопрос гостя записан, ответа нет — last_id = id вопроса (иначе
#      опрос показал бы вопрос второй раз под сообщением об ошибке).
#
# Сети и LLM не требуется: get_user_rag подменяется заглушкой.
# Запуск:  venv/Scripts/python.exe tests/test_widget_lastid.py   (сервер: venv/bin/python ...)

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import auth_db
import web_app

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    line = f"  [{'OK  ' if cond else 'FAIL'}] {name}"
    if not cond and extra != '':
        line += f'  -> {extra!r}'
    print(line)


class _Settings:
    """Заглушка RAGSettings: get/reload + ключ (реальный LLM не нужен)."""

    def __init__(self, **overrides):
        self.db = {'llm_model': 'test-model', 'llm_base_url': 'http://127.0.0.1:9/v1'}
        self.db.update(overrides)
        self.data = dict(self.db)

    def reload(self):
        self.data = dict(self.db)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return 'test-key'


class _FakeRAG:
    def __init__(self):
        self.settings = _Settings()
        self.calls = []
        self.fail = False

    def ask_model(self, question, user_prompt=None, history=None):
        self.calls.append({'question': question, 'history': history})
        if self.fail:
            raise RuntimeError('модель недоступна (тест)')
        return 'ОТВЕТ ВИДЖЕТА'


def _make_test_user():
    name = 'testwid_' + uuid.uuid4().hex[:8]
    ok, msg = auth_db.register_user(name, 'test-pass-123')
    if not ok:
        raise RuntimeError(f'не удалось создать тест-пользователя: {msg}')
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT id FROM users WHERE username = %s', (name,))
    uid = cur.fetchone()[0]
    cur.close()
    conn.close()
    return uid, name


def _history(client, w, visitor, after_id=None):
    url = '/api/widget/history?key=%s&visitor_id=%s&site=' % (w['key'], visitor)
    if after_id is not None:
        url += '&after_id=%d' % after_id
    r = client.get(url)
    return r, (r.get_json() or {}).get('messages', [])


def main():
    auth_db.init_db()
    auth_db.init_chat_history()
    uid, name = _make_test_user()
    print(f'Тестовый пользователь: {name} (id={uid})')

    fake = _FakeRAG()
    web_app.get_user_rag = lambda user_id: fake
    web_app.rag_ready = True
    client = web_app.app.test_client()
    web_app._rate_hits.clear()

    ok, w = auth_db.create_widget(uid, 'test-lastid-widget')
    print(f"Тестовый виджет: #{w['id'] if ok else '—'}")
    visitor = 'visitor_' + uuid.uuid4().hex[:12]
    scope = 'wid:%d:%s' % (w['id'], visitor)
    body = {'key': w['key'], 'visitor_id': visitor, 'site': '', 'question': 'Добрый день'}
    # id последней реплики, которую гость видел ДО вопроса: с ним опрашивал ленту
    # прежний клиент (у нового посетителя ленты нет — 0)
    seen_before = auth_db.get_widget_history(uid, scope)
    poll_id = seen_before[-1]['id'] if seen_before else 0

    try:
        # === 1) Ответ виджета несёт last_id ===
        print('\n1) /api/widget/ask отдаёт last_id')
        r = client.post('/api/widget/ask', json=body)
        d = r.get_json() or {}
        rows = auth_db.get_widget_history(uid, scope)
        check('виджет ответил', r.status_code == 200 and d.get('answer') == 'ОТВЕТ ВИДЖЕТА',
              r.get_data(as_text=True)[:200])
        check('last_id = id последней записанной реплики',
              d.get('last_id') and rows and d['last_id'] == rows[-1]['id'],
              {'last_id': d.get('last_id'), 'rows': [(m['id'], m['role']) for m in rows]})
        check('в ленте ровно вопрос и ответ (дубля в БД нет)',
              [m['role'] for m in rows] == ['user', 'assistant'],
              [(m['id'], m['role']) for m in rows])

        # === 2) Опрос ленты этим last_id ничего не задваивает ===
        print('\n2) Опрос ленты после ответа')
        _, empty = _history(client, w, visitor, after_id=d['last_id'])
        check('опрос с after_id=last_id молчит (дубля нет)', empty == [], empty)
        _, old = _history(client, w, visitor, after_id=poll_id)
        check('с прежним after_id (%s) опрос вернул обе реплики — так и появлялся дубль' % poll_id,
              [m['role'] for m in old] == ['user', 'assistant'],
              [(m['id'], m['role']) for m in old])

        # === 3) Диалог ведёт менеджер ===
        print('\n3) Ручной режим: вопрос гостя без ответа бота')
        # Диалог попадает в реестр бэкфиллом (так его видит владелец в «Диалогах»),
        # затем менеджер берёт диалог на себя
        auth_db.sync_conversations('wid', 'wid:%d:%%' % w['id'])
        check('диалог виден в панели (реестр conversations)',
              auth_db.set_widget_dialog_mode(uid, w['id'], visitor, 'human') is not False,
              auth_db.widget_conversation_state(uid, w['id'], visitor))
        before = len(auth_db.get_widget_history(uid, scope))
        r = client.post('/api/widget/ask', json=dict(body, question='Менеджер, нужна помощь'))
        d3 = r.get_json() or {}
        rows = auth_db.get_widget_history(uid, scope)
        check('гость получил «менеджер смотрит вопрос»', d3.get('human') and d3.get('answer'),
              d3)
        check('записана только реплика гостя (+1) и last_id указывает на неё',
              len(rows) == before + 1 and d3.get('last_id') == rows[-1]['id']
              and rows[-1]['role'] == 'user',
              {'n': len(rows), 'last_id': d3.get('last_id'), 'role': rows[-1]['role']})
        _, empty3 = _history(client, w, visitor, after_id=d3['last_id'])
        check('опрос после ручного режима молчит', empty3 == [], empty3)
        auth_db.set_widget_dialog_mode(uid, w['id'], visitor, 'bot')

        # === 4) Исчерпанный дневной лимит ===
        print('\n4) Дневной лимит исчерпан')
        auth_db.update_widget(uid, w['id'], {'daily_limit': 0})
        before = len(auth_db.get_widget_history(uid, scope))
        r = client.post('/api/widget/ask', json=dict(body, question='вопрос сверх лимита'))
        d4 = r.get_json() or {}
        check('лимит: учтивый ответ и ничего не записано',
              r.status_code == 200 and 'лимит' in (d4.get('answer') or '')
              and len(auth_db.get_widget_history(uid, scope)) == before,
              d4)
        check('лимит: last_id = 0 (показывать нечего)', not d4.get('last_id'), d4)

        # === 5) Сбой модели: вопрос записан, ответа нет ===
        print('\n5) Сбой модели')
        auth_db.update_widget(uid, w['id'], {'daily_limit': 100})
        fake.fail = True
        before = len(auth_db.get_widget_history(uid, scope))
        r = client.post('/api/widget/ask', json=dict(body, question='вопрос при сбое'))
        d5 = r.get_json() or {}
        rows = auth_db.get_widget_history(uid, scope)
        check('сбой: гость получает текст ошибки (500)', r.status_code == 500 and d5.get('error'), d5)
        check('сбой: вопрос гостя остался в ленте, last_id — на нём',
              len(rows) == before + 1 and d5.get('last_id') == rows[-1]['id'],
              {'n': len(rows), 'last_id': d5.get('last_id')})
        _, empty5 = _history(client, w, visitor, after_id=d5['last_id'])
        check('сбой: опрос не повторяет уже показанный вопрос', empty5 == [], empty5)
        fake.fail = False
    finally:
        conn = auth_db.get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM chat_history WHERE user_id = %s AND device_id = %s', (uid, scope))
        conn.commit()
        cur.close()
        conn.close()
        auth_db.delete_user(uid)
        print(f'\nТест-пользователь {name} удалён (вместе с его виджетом и лентой)')

    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for n in FAILED:
        print(f'  ❌ {n}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
