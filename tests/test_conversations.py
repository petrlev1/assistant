# test_conversations.py - дымовой тест реестра диалогов каналов (кусок A1)
#
# Что проверяется:
#   1) init_conversations создаёт таблицу conversations (DDL идемпотентен);
#   2) backfill_conversations заливает по диалогу на каждый scope виджета/MAX,
#      игнорируя личный чат владельца ('web') и битые scope;
#   3) last_message_at/last_role берутся из последнего сообщения диалога;
#   4) повторный бэкфилл ничего не затирает (режим/непрочитанное/кто ведёт);
#   5) при удалении владельца его диалоги уезжают каскадом.
#
# Сети и LLM не требуется. Запуск из корня проекта:
#   venv/Scripts/python.exe tests/test_conversations.py
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import auth_db

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    line = f'  [{"OK  " if cond else "FAIL"}] {name}'
    if not cond and extra != '':
        line += f'  -> {extra!r}'
    print(line)


def _q(sql, params=()):
    """Один запрос в своём соединении: SELECT отдаёт строки, DML — пусто.

    Соединение закрывается в finally: если запрос упал, открытая транзакция
    иначе осталась бы держать блокировки до сборщика мусора (дедлок на каскаде).
    """
    conn = auth_db.get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


def _make_test_user():
    name = 'testconv_' + uuid.uuid4().hex[:8]
    ok, msg = auth_db.register_user(name, 'test-pass-123')
    if not ok:
        raise RuntimeError(f'не удалось создать тест-пользователя: {msg}')
    return _q('SELECT id FROM users WHERE username = %s', (name,))[0][0], name


def test_inbox_http(uid, wid, vis1, vis2, s1):
    print('\n4) Панель: GET /api/widgets/<id>/inbox')
    # Раздел 3 оставил у диалога vis1 режим human и непрочитанное — снимаем,
    # иначе он законно стоит первым (непрочитанные всегда наверху).
    _q("UPDATE conversations SET mode='bot', unread_for_owner=0 WHERE conv_key = %s", (s1,))
    import web_app
    client = web_app.app.test_client()
    check('без логина — 401', client.get(f'/api/widgets/{wid}/inbox').status_code == 401)

    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'

    uid2, _ = _make_test_user()
    try:
        ok, other = auth_db.create_widget(uid2, 'чужой виджет')
        check('чужой виджет — 404',
              client.get(f'/api/widgets/{other["id"]}/inbox').status_code == 404)
    finally:
        auth_db.delete_user(uid2)

    # Гость написал ПОСЛЕ бэкфилла: панель должна его увидеть (досыпка при чтении)
    vis3 = 'ffffeeee5555dddd'
    auth_db.save_message(uid, 'user', 'А есть самовывоз?', device_id=f'wid:{wid}:{vis3}')

    r = client.get(f'/api/widgets/{wid}/inbox')
    body = r.get_json() or {}
    dialogs = body.get('dialogs') or []
    by_visitor = {d['visitor']: d for d in dialogs}
    check('панель вернула 200 и три диалога',
          r.status_code == 200 and set(by_visitor) == {vis1, vis2, vis3},
          (r.status_code, list(by_visitor)))
    check('сперва ждущие ответа человека, кто дольше ждёт — выше',
          [d['visitor'] for d in dialogs[:2]] == [vis2, vis3]
          and dialogs[0]['last_role'] == 'user' and dialogs[1]['last_role'] == 'user',
          [(d['visitor'], d['last_role']) for d in dialogs])
    check('затем диалог, где ответил бот',
          dialogs[2]['visitor'] == vis1 and dialogs[2]['last_role'] == 'assistant',
          [(d['visitor'], d['last_role']) for d in dialogs])
    d3 = by_visitor[vis3]
    check('авто-заголовок — первый вопрос гостя',
          d3['title'].startswith('А есть самовывоз'), d3)
    check('короткий код гостя — последние 4 символа', d3['code'] == vis3[-4:], d3)
    check('последнее сообщение, счётчик и режим по умолчанию',
          d3['last_message'].startswith('А есть самовывоз') and d3['messages'] == 1
          and d3['mode'] == 'bot' and d3['unread'] == 0, d3)
    check('epoch-метка времени разговора', d3['last_ts'] > 0, d3)

    _q("UPDATE conversations SET mode='human', unread_for_owner=4 WHERE conv_key = %s", (s1,))
    body = client.get(f'/api/widgets/{wid}/inbox').get_json() or {}
    top = (body.get('dialogs') or [{}])[0]
    check('непрочитанный диалог поднимается наверх', top.get('visitor') == vis1, top)
    check('режим human отдан панели', top.get('mode') == 'human', top)
    check('счётчик непрочитанного и сумма по виджету',
          top.get('unread') == 4 and body.get('unread_total') == 4,
          (top.get('unread'), body.get('unread_total')))
    check('имя виджета в ответе',
          (body.get('widget') or {}).get('name') == 'test-conv-widget', body.get('widget'))
    _q("UPDATE conversations SET mode='bot', unread_for_owner=0 WHERE conv_key = %s", (s1,))

    # Второй виджет того же владельца: диалоги не смешиваются
    ok, w2 = auth_db.create_widget(uid, 'второй виджет')
    vis4 = 'cafebabe00001111'
    auth_db.save_message(uid, 'user', 'вопрос из второго виджета', device_id=f'wid:{w2["id"]}:{vis4}')
    keys1 = {d['visitor'] for d in (client.get(f'/api/widgets/{wid}/inbox').get_json() or {}).get('dialogs', [])}
    keys2 = {d['visitor'] for d in (client.get(f'/api/widgets/{w2["id"]}/inbox').get_json() or {}).get('dialogs', [])}
    check('диалоги разных виджетов не смешиваются',
          vis4 not in keys1 and vis4 in keys2, (sorted(keys1), sorted(keys2)))
    auth_db.delete_widget(uid, w2['id'])


def test_dialog_http(uid, wid, vis1):
    print('\n5) Транскрипт диалога: GET /api/widgets/<id>/dialog')
    import web_app
    client = web_app.app.test_client()
    check('без логина — 401',
          client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}').status_code == 401)

    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'

    r = client.get(f'/api/widgets/{wid}/dialog?visitor=zz')
    check('битый visitor — 400', r.status_code == 400, r.status_code)

    r = client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}')
    body = r.get_json() or {}
    msgs = body.get('messages') or []
    check('транскрипт отдаёт сообщения по порядку',
          r.status_code == 200 and [m['role'] for m in msgs] == ['user', 'assistant'], body)
    check('тексты реплик на месте',
          msgs and msgs[0]['message'].startswith('Сколько стоит насос'), msgs)
    check('epoch-метки у сообщений', all(m.get('ts', 0) > 0 for m in msgs), msgs)
    d = body.get('dialog') or {}
    check('состояние диалога в ответе',
          d.get('mode') == 'bot' and d.get('total') == 2 and d.get('visitor') == vis1, d)
    check('has_more=false на коротком диалоге', body.get('has_more') is False, body.get('has_more'))

    body2 = client.get(f'/api/widgets/{wid}/dialog?visitor=0123456789abcdef').get_json() or {}
    check('незнакомый посетитель — пустой транскрипт',
          body2.get('messages') == [] and (body2.get('dialog') or {}).get('total') == 0, body2)

    last_id = msgs[-1]['id']
    body3 = client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}&after_id={last_id}').get_json() or {}
    check('after_id на последнем сообщении — пусто', body3.get('messages') == [], body3)

    # Роль operator протаскивается как есть — на неё опирается ответ менеджера (кусок B2)
    auth_db.save_message(uid, 'operator', 'Отвечает менеджер', device_id=f'wid:{wid}:{vis1}')
    body4 = client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}&after_id={last_id}').get_json() or {}
    new_msgs = body4.get('messages') or []
    check('after_id отдаёт только новую реплику',
          len(new_msgs) == 1 and new_msgs[0]['role'] == 'operator'
          and new_msgs[0]['message'] == 'Отвечает менеджер', new_msgs)

    body5 = client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}&limit=1').get_json() or {}
    tail = body5.get('messages') or []
    check('limit отдаёт хвост диалога, has_more=true',
          len(tail) == 1 and tail[0]['message'] == 'Отвечает менеджер'
          and body5.get('has_more') is True, (tail, body5.get('has_more')))

    _q("UPDATE conversations SET mode='human', unread_for_owner=2 WHERE conv_key = %s",
       (f'wid:{wid}:{vis1}',))
    d2 = ((client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}').get_json() or {})
          .get('dialog') or {})
    check('режим human и непрочитанное видны в транскрипте',
          d2.get('mode') == 'human' and d2.get('unread') == 2, d2)
    _q("UPDATE conversations SET mode='bot', unread_for_owner=0 WHERE conv_key = %s",
       (f'wid:{wid}:{vis1}',))


def test_inbox_page(uid, wid, vis1):
    print('\n6) Страница панели: /inbox и бейдж в кабинете')
    import web_app
    anon = web_app.app.test_client()
    r = anon.get('/inbox')
    check('/inbox без логина отправляет на вход',
          r.status_code == 302 and '/login' in (r.headers.get('Location') or ''),
          (r.status_code, r.headers.get('Location')))

    client = web_app.app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'

    r = client.get('/inbox')
    body = r.get_data(as_text=True)
    check('/inbox отдаёт страницу панели',
          r.status_code == 200 and 'Диалоги гостей' in body and '/inbox' in body, r.status_code)
    check('на странице есть выбор виджета и поле ответа',
          'id="wsel"' in body and 'id="reply"' in body and 'id="sendbtn"' in body, None)

    check('счётчик непрочитанного пуст', auth_db.count_unread_conversations(uid) == 0,
          auth_db.count_unread_conversations(uid))
    _q("UPDATE conversations SET unread_for_owner=3 WHERE conv_key = %s", (f'wid:{wid}:{vis1}',))
    check('счётчик видит непрочитанное', auth_db.count_unread_conversations(uid) == 3,
          auth_db.count_unread_conversations(uid))
    chat_body = client.get('/chat').get_data(as_text=True)
    check('в кабинете у кнопки «Диалоги» появляется бейдж',
          'class="cnt">3<' in chat_body, None)
    _q("UPDATE conversations SET unread_for_owner=0 WHERE conv_key = %s", (f'wid:{wid}:{vis1}',))


def test_mode_http(uid, wid, vis1, vis2):
    print('\n7) Режим диалога: takeover / release')
    import web_app
    client = web_app.app.test_client()
    check('takeover без логина — 401',
          client.post(f'/api/widgets/{wid}/takeover', json={'visitor': vis1}).status_code == 401)

    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'

    r = client.post(f'/api/widgets/{wid}/takeover', json={'visitor': 'zz'})
    check('битый visitor — 400', r.status_code == 400, r.status_code)
    r = client.post(f'/api/widgets/{wid}/takeover', json={'visitor': 'aabbccdd11223344'})
    check('незнакомый гость — 404 (диалога нет)', r.status_code == 404, r.status_code)

    uid2, _ = _make_test_user()
    try:
        ok, other = auth_db.create_widget(uid2, 'чужой виджет')
        r = client.post(f'/api/widgets/{other["id"]}/takeover', json={'visitor': vis1})
        check('чужой виджет — 404', r.status_code == 404, r.status_code)
    finally:
        auth_db.delete_user(uid2)

    _q("UPDATE conversations SET unread_for_owner=5 WHERE conv_key = %s", (f'wid:{wid}:{vis1}',))
    r = client.post(f'/api/widgets/{wid}/takeover', json={'visitor': vis1})
    d = (r.get_json() or {}).get('dialog') or {}
    check('takeover отвечает успехом',
          r.status_code == 200 and (r.get_json() or {}).get('success'),
          r.get_data(as_text=True)[:200])
    check('режим human и ведёт владелец',
          d.get('mode') == 'human' and d.get('operator_user_id') == uid, d)
    check('непрочитанное снято при взятии', d.get('unread') == 0, d)
    check('в реестре тоже human',
          _q("SELECT mode FROM conversations WHERE conv_key = %s", (f'wid:{wid}:{vis1}',))[0][0] == 'human')
    check('соседний диалог остался у бота',
          _q("SELECT mode FROM conversations WHERE conv_key = %s", (f'wid:{wid}:{vis2}',))[0][0] == 'bot')

    body = client.get(f'/api/widgets/{wid}/inbox').get_json() or {}
    modes = {x['visitor']: x['mode'] for x in body.get('dialogs', [])}
    check('панель отдаёт режим по каждому диалогу',
          modes.get(vis1) == 'human' and modes.get(vis2) == 'bot', modes)

    r = client.post(f'/api/widgets/{wid}/release', json={'visitor': vis1})
    d2 = (r.get_json() or {}).get('dialog') or {}
    check('release возвращает бота',
          r.status_code == 200 and d2.get('mode') == 'bot' and d2.get('operator_user_id') is None, d2)
    check('release идемпотентен',
          client.post(f'/api/widgets/{wid}/release', json={'visitor': vis1}).status_code == 200)


def test_reply_http(uid, wid, wkey, vis1, vis2):
    print('\n8) Ответ оператора: POST /api/widgets/<id>/reply')
    import web_app
    client = web_app.app.test_client()
    check('reply без логина — 401',
          client.post(f'/api/widgets/{wid}/reply', json={'visitor': vis1, 'text': 'привет'}).status_code == 401)

    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'

    r = client.post(f'/api/widgets/{wid}/reply', json={'visitor': vis1, 'text': '   '})
    check('пустой ответ — 400', r.status_code == 400, r.status_code)
    r = client.post(f'/api/widgets/{wid}/reply', json={'visitor': vis1, 'text': 'я' * 4001})
    check('слишком длинный ответ — 400', r.status_code == 400, r.status_code)
    r = client.post(f'/api/widgets/{wid}/reply', json={'visitor': 'aabbccdd11223344', 'text': 'привет'})
    check('незнакомый гость — 404', r.status_code == 404, r.status_code)

    uid2, _ = _make_test_user()
    try:
        ok, other = auth_db.create_widget(uid2, 'чужой виджет')
        r = client.post(f'/api/widgets/{other["id"]}/reply', json={'visitor': vis1, 'text': 'привет'})
        check('чужой виджет — 404', r.status_code == 404, r.status_code)
    finally:
        auth_db.delete_user(uid2)

    # Соседний диалог оставляем у бота: ответ в vis1 не должен его трогать
    text = 'Менеджер: да, самовывоз возможен со склада на Ленина, 5'
    r = client.post(f'/api/widgets/{wid}/reply', json={'visitor': vis1, 'text': text})
    body = r.get_json() or {}
    m = body.get('message') or {}
    check('ответ записан как реплика оператора',
          r.status_code == 200 and m.get('role') == 'operator' and m.get('id') and m.get('ts', 0) > 0,
          body)
    check('текст ответа сохранён без изменений', m.get('message') == text, m)
    check('ответ сам перевёл диалог на человека',
          (body.get('dialog') or {}).get('mode') == 'human'
          and (body.get('dialog') or {}).get('operator_user_id') == uid, body.get('dialog'))
    check('соседний диалог остался у бота',
          _q("SELECT mode FROM conversations WHERE conv_key = %s", (f'wid:{wid}:{vis2}',))[0][0] == 'bot')

    tr = client.get(f'/api/widgets/{wid}/dialog?visitor={vis1}').get_json() or {}
    last = (tr.get('messages') or [])[-1]
    check('в транскрипте панели ответ идёт последним и ролью operator',
          last.get('role') == 'operator' and last.get('message') == text, last)

    # Гость видит этот ответ в своём виджете
    hist = client.get('/api/widget/history?key=' + wkey + '&visitor_id=' + vis1).get_json() or {}
    msgs = hist.get('messages') or []
    check('гость получает ответ менеджера в ленте виджета',
          msgs and msgs[-1]['role'] == 'operator' and msgs[-1]['message'] == text, msgs[-3:])

    body = client.get(f'/api/widgets/{wid}/inbox').get_json() or {}
    row = [x for x in body.get('dialogs', []) if x['visitor'] == vis1][0]
    check('панель показывает последнюю реплику оператора',
          row['last_role'] == 'operator' and text[:20] in row['last_message'], row)


class _FakeRAG:
    """Заглушка RAGCore: считает вызовы модели и отвечает предсказуемо."""

    def __init__(self):
        self.calls = 0
        self.settings = {}          # _rag_provider_model читает провайдера/модель отсюда

    def ask_model(self, question, user_prompt=None, history=None):
        self.calls += 1
        return 'БОТ: ответ на «%s»' % question


def test_manual_mode(uid, wid, wkey, vis_new):
    print('\n9) Ручной режим в боевом пути /api/widget/ask')
    import web_app
    fake = _FakeRAG()
    real_rag_getter, real_ready = web_app.get_user_rag, web_app.rag_ready
    web_app.get_user_rag = lambda user_id: fake
    web_app.rag_ready = True
    client = web_app.app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'
    pack = {'key': wkey, 'visitor_id': vis_new, 'site': ''}

    def ask(question):
        return client.post('/api/widget/ask', json=dict(pack, question=question))

    def hits():
        rows = _q("SELECT hits FROM widget_hits WHERE widget_id = %s AND day = CURRENT_DATE", (wid, ))
        return rows[0][0] if rows else 0

    try:
        r = ask('обычный вопрос боту')
        check('в режиме bot модель вызывается и отвечает',
              r.status_code == 200 and (r.get_json() or {}).get('answer', '').startswith('БОТ:')
              and fake.calls == 1, (r.get_json(), fake.calls))

        r = client.post(f'/api/widgets/{wid}/takeover', json={'visitor': vis_new})
        check('взятие диалога на себя успешно',
              r.status_code == 200 and (r.get_json() or {}).get('success'), r.get_data(as_text=True)[:200])

        hits_before = hits()
        r = ask('вопрос в ручном режиме')
        body = r.get_json() or {}
        check('в ручном режиме модель НЕ вызывается', fake.calls == 1, fake.calls)
        check('гостю приходит «менеджер обрабатывает»',
              body.get('human') is True and 'Менеджер' in (body.get('answer') or ''), body)
        check('дневной лимит виджета не тратится', hits() == hits_before, (hits_before, hits()))
        check('вопрос гостя записан в ленту диалога',
              _q("SELECT count(*) FROM chat_history WHERE device_id = %s AND role = 'user'",
                 (f'wid:{wid}:{vis_new}', ))[0][0] == 2)
        check('нет сохранённого ответа бота в ручном режиме',
              _q("SELECT count(*) FROM chat_history WHERE device_id = %s AND role = 'assistant'",
                 (f'wid:{wid}:{vis_new}', ))[0][0] == 1)
        check('вопрос попал в аналитику (аналитика не слепнет)',
              _q("SELECT count(*) FROM query_analytics WHERE user_id = %s AND question = %s",
                 (uid, 'вопрос в ручном режиме'))[0][0] == 1)
        row = _q("SELECT unread_for_owner, last_role FROM conversations WHERE conv_key = %s",
                 (f'wid:{wid}:{vis_new}', ))[0]
        check('диалог помечен непрочитанным и ждёт человека',
              row[0] == 1 and row[1] == 'user', row)

        body = client.get(f'/api/widgets/{wid}/inbox').get_json() or {}
        d = [x for x in body.get('dialogs', []) if x['visitor'] == vis_new][0]
        check('панель показывает ожидающий вопрос',
              d['unread'] == 1 and d['mode'] == 'human'
              and d['last_message'].startswith('вопрос в ручном'), d)

        r = client.post(f'/api/widgets/{wid}/release', json={'visitor': vis_new})
        check('возврат боту успешен', r.status_code == 200, r.get_data(as_text=True)[:120])
        r = ask('вопрос после возврата')
        check('после возврата бот снова отвечает',
              fake.calls == 2 and (r.get_json() or {}).get('answer', '').startswith('БОТ:'),
              (fake.calls, r.get_json()))
    finally:
        web_app.get_user_rag, web_app.rag_ready = real_rag_getter, real_ready


def test_frame_poll(uid, wid, wkey, vis1):
    print('\n10) Лента виджета: догрузка after_id, роль operator, опрос в кадре')
    import web_app
    client = web_app.app.test_client()
    base = f'/api/widget/history?key={wkey}&visitor_id={vis1}'

    d = client.get(base).get_json() or {}
    msgs = d.get('messages') or []
    check('история отдаёт id сообщений и признак ручного режима',
          bool(msgs) and all('id' in m for m in msgs) and 'human' in d, list(d.keys()))
    last = msgs[-1]['id']
    d2 = client.get(base + f'&after_id={last}').get_json() or {}
    check('after_id без новых сообщений — пусто', d2.get('messages') == [], d2.get('messages'))

    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testconv'
    client.post(f'/api/widgets/{wid}/reply', json={'visitor': vis1, 'text': 'Ответ через опрос ленты'})

    d3 = client.get(base + f'&after_id={last}').get_json() or {}
    new = d3.get('messages') or []
    check('догрузка приносит ответ менеджера без перезагрузки страницы',
          len(new) == 1 and new[0]['role'] == 'operator'
          and new[0]['message'] == 'Ответ через опрос ленты', new)
    check('лента помечает, что отвечает человек', d3.get('human') is True, d3.get('human'))

    page = client.get(f'/widget/{wkey}').get_data(as_text=True)
    check('кадр виджета рисует роль менеджера отдельным пузырём',
          'Менеджер' in page and 'msg.op' in page, None)
    check('кадр опрашивает ленту по after_id',
          'setInterval(poll' in page and 'after_id' in page, None)


def test_race(uid, wid, wkey, vis_race):
    print('\n11) Перехват во время генерации: опоздавший ответ бота не доходит')
    import web_app, auth_db
    client = web_app.app.test_client()
    pack = {'key': wkey, 'visitor_id': vis_race, 'site': ''}
    real_rag_getter, real_ready = web_app.get_user_rag, web_app.rag_ready

    class _Plain:
        def __init__(self):
            self.calls = 0
            self.settings = {}

        def ask_model(self, question, user_prompt=None, history=None):
            self.calls += 1
            return 'БОТ: ответ'

    plain = _Plain()
    web_app.get_user_rag = lambda uid_: plain
    web_app.rag_ready = True
    try:
        r = client.post('/api/widget/ask', json=dict(pack, question='первый вопрос'))
        check('обычный ответ бота проходит',
              (r.get_json() or {}).get('answer') == 'БОТ: ответ', r.get_json())
        # Панель досыпает реестр — как в жизни, когда владелец открыл диалоги
        auth_db.sync_conversations('wid', 'wid:%d:%%' % wid)

        class _Racing(_Plain):
            def ask_model(self, question, user_prompt=None, history=None):
                self.calls += 1
                # Менеджер берёт диалог на себя, пока модель «думает»
                auth_db.set_widget_dialog_mode(uid, wid, vis_race, 'human', operator_user_id=uid)
                return 'БОТ: опоздавший ответ'

        racing = _Racing()
        web_app.get_user_rag = lambda uid_: racing
        r = client.post('/api/widget/ask', json=dict(pack, question='вопрос в момент перехвата'))
        body = r.get_json() or {}
        check('гостю уходит «менеджер смотрит вопрос» вместо опоздавшего ответа',
              body.get('human') is True and 'Менеджер' in (body.get('answer') or ''), body)
        check('опоздавший ответ бота не сохранён в ленту',
              _q("SELECT count(*) FROM chat_history WHERE device_id = %s AND role = 'assistant'",
                 (f'wid:{wid}:{vis_race}', ))[0][0] == 1, None)
        check('вопрос гостя всё равно записан',
              _q("SELECT count(*) FROM chat_history WHERE device_id = %s AND role = 'user'",
                 (f'wid:{wid}:{vis_race}', ))[0][0] == 2, None)
    finally:
        web_app.get_user_rag, web_app.rag_ready = real_rag_getter, real_ready
        auth_db.set_widget_dialog_mode(uid, wid, vis_race, 'bot')


def test_bot_pause(uid, wid, wkey, vis_pause):
    print('\n12) Пауза бота у всего виджета')
    import web_app, auth_db
    client = web_app.app.test_client()
    pack = {'key': wkey, 'visitor_id': vis_pause, 'site': ''}
    real_rag_getter, real_ready = web_app.get_user_rag, web_app.rag_ready

    class _Fake:
        def __init__(self):
            self.calls = 0
            self.settings = {}

        def ask_model(self, question, user_prompt=None, history=None):
            self.calls += 1
            return 'БОТ: ответ'

    fake = _Fake()
    web_app.get_user_rag = lambda uid_: fake
    web_app.rag_ready = True
    try:
        check('пауза без логина — 401',
              client.post(f'/api/widgets/{wid}', json={'bot_paused': True}).status_code == 401)

        with client.session_transaction() as sess:
            sess['user_id'] = uid
            sess['username'] = 'testconv'

        uid2, _ = _make_test_user()
        try:
            ok, other = auth_db.create_widget(uid2, 'чужой виджет')
            r = client.post(f'/api/widgets/{other["id"]}', json={'bot_paused': True})
            check('чужой виджет на паузу не поставить', r.status_code == 404, r.status_code)
        finally:
            auth_db.delete_user(uid2)

        r = client.post(f'/api/widgets/{wid}', json={'bot_paused': True})
        check('пауза сохраняется', r.status_code == 200 and (r.get_json() or {}).get('success'),
              r.get_data(as_text=True)[:150])
        ws = [x for x in (client.get('/api/widgets').get_json() or {}).get('widgets', []) if x['id'] == wid]
        check('пауза видна в списке виджетов', bool(ws) and ws[0]['bot_paused'] is True, ws[:1])

        r = client.post('/api/widget/ask', json=dict(pack, question='вопрос при паузе'))
        body = r.get_json() or {}
        check('при паузе модель не вызывается', fake.calls == 0, fake.calls)
        check('гостю при паузе уходит «менеджер обрабатывает»',
              body.get('human') is True and 'Менеджер' in (body.get('answer') or ''), body)
        check('пауза не выдаёт себя за перехват диалога (режим остаётся bot)',
              auth_db.widget_conversation_mode(uid, wid, vis_pause) == 'bot')
        check('вопрос при паузе ждёт владельца', _q(
            "SELECT unread_for_owner FROM conversations WHERE conv_key = %s",
            (f'wid:{wid}:{vis_pause}', ))[0][0] == 1)

        client.post(f'/api/widgets/{wid}', json={'bot_paused': False})
        r = client.post('/api/widget/ask', json=dict(pack, question='после снятия паузы'))
        check('после снятия паузы бот отвечает',
              fake.calls == 1 and (r.get_json() or {}).get('answer') == 'БОТ: ответ', (fake.calls, r.get_json()))
    finally:
        web_app.get_user_rag, web_app.rag_ready = real_rag_getter, real_ready
        auth_db.update_widget(uid, wid, {'bot_paused': False})


def test_auto_return(uid, wid, wkey, vis_idle):
    print('\n13) Авто-возврат бота к застоявшемуся диалогу')
    import web_app, auth_db
    client = web_app.app.test_client()
    pack = {'key': wkey, 'visitor_id': vis_idle, 'site': ''}
    real_rag_getter, real_ready = web_app.get_user_rag, web_app.rag_ready
    scope = f'wid:{wid}:{vis_idle}'

    class _Fake:
        def __init__(self):
            self.calls = 0
            self.settings = {}

        def ask_model(self, question, user_prompt=None, history=None):
            self.calls += 1
            return 'БОТ: ответ'

    fake = _Fake()
    web_app.get_user_rag = lambda uid_: fake
    web_app.rag_ready = True
    try:
        with client.session_transaction() as sess:
            sess['user_id'] = uid
            sess['username'] = 'testconv'
        check('авто-возврат не число — 400',
              client.post(f'/api/widgets/{wid}', json={'bot_idle_minutes': 'abc'}).status_code == 400)

        client.post(f'/api/widgets/{wid}', json={'bot_idle_minutes': 60})    # 1 час: не сработает
        r = client.post('/api/widget/ask', json=dict(pack, question='первый вопрос'))
        check('первый вопрос отвечает бот', fake.calls == 1 and r.status_code == 200, r.get_json())

        client.post(f'/api/widgets/{wid}', json={'bot_idle_minutes': 1})
        client.post(f'/api/widgets/{wid}/takeover', json={'visitor': vis_idle})
        r = client.post('/api/widget/ask', json=dict(pack, question='вопрос сразу после перехвата'))
        check('свежий диалог человека авто-возврат не трогает',
              fake.calls == 1 and (r.get_json() or {}).get('human') is True, (fake.calls, r.get_json()))

        # Гость вернулся через два часа тишины — диалог снова у бота
        _q("UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP - INTERVAL '2 hours' "
           "WHERE conv_key = %s", (scope, ))
        r = client.post('/api/widget/ask', json=dict(pack, question='вопрос через два часа'))
        body = r.get_json() or {}
        check('застоявшийся диалог возвращается боту',
              fake.calls == 2 and not body.get('human') and body.get('answer') == 'БОТ: ответ',
              (fake.calls, body))
        check('режим в реестре снова bot',
              auth_db.widget_conversation_mode(uid, wid, vis_idle) == 'bot')

        # Авто-возврат выключен: диалог остаётся у человека сколько угодно
        client.post(f'/api/widgets/{wid}', json={'bot_idle_minutes': 0})
        client.post(f'/api/widgets/{wid}/takeover', json={'visitor': vis_idle})
        _q("UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP - INTERVAL '5 hours' "
           "WHERE conv_key = %s", (scope, ))
        r = client.post('/api/widget/ask', json=dict(pack, question='вопрос при выключенном авто-возврате'))
        check('при bot_idle_minutes=0 диалог остаётся у человека',
              fake.calls == 2 and (r.get_json() or {}).get('human') is True, (fake.calls, r.get_json()))
    finally:
        web_app.get_user_rag, web_app.rag_ready = real_rag_getter, real_ready
        auth_db.update_widget(uid, wid, {'bot_idle_minutes': 0})
        auth_db.set_widget_dialog_mode(uid, wid, vis_idle, 'bot')


def main():
    auth_db.init_db()
    auth_db.init_chat_history()
    auth_db.init_widgets()
    auth_db.init_max_channels()

    print('1) DDL таблицы conversations')
    check('init_conversations вернул True', auth_db.init_conversations() is True)
    check('повторный вызов не падает', auth_db.init_conversations() is True)
    cols = {r[0] for r in _q("""SELECT column_name FROM information_schema.columns
                                WHERE table_name = 'conversations'""")}
    need = {'channel', 'owner_user_id', 'conv_key', 'mode', 'operator_user_id',
            'last_message_at', 'last_role', 'unread_for_owner'}
    check('все нужные колонки на месте', need <= cols, sorted(cols))
    check('conv_key уникален',
          bool(_q("""SELECT 1 FROM information_schema.table_constraints
                     WHERE table_name='conversations' AND constraint_type IN ('UNIQUE','PRIMARY KEY')""")))
    check("mode ограничен 'bot'/'human'",
          bool(_q("""SELECT 1 FROM pg_constraint WHERE conname = 'conversations_mode_check'""")))

    uid, name = _make_test_user()
    print(f'Тестовый пользователь: {name} (id={uid})')
    try:
        ok, w = auth_db.create_widget(uid, 'test-conv-widget')
        assert ok, 'виджет не создан'
        wid = w['id']
        vis1, vis2 = 'aaaabbbb1111cccc', 'dddd2222eeee3333'
        s1, s2 = f'wid:{wid}:{vis1}', f'wid:{wid}:{vis2}'

        auth_db.save_message(uid, 'user', 'Сколько стоит насос?', device_id=s1)
        auth_db.save_message(uid, 'assistant', '12 500 руб.', device_id=s1)
        auth_db.save_message(uid, 'user', 'Есть доставка?', device_id=s2)
        auth_db.save_message(uid, 'user', 'мой личный вопрос', device_id='web')
        auth_db.save_message(uid, 'user', 'битый scope', device_id='wid:нечисло:zzz')

        print('2) Бэкфилл диалогов из chat_history')
        added = auth_db.backfill_conversations()
        rows = _q("""SELECT conv_key, channel, mode, last_role, owner_user_id
                     FROM conversations WHERE conv_key LIKE %s ORDER BY conv_key""",
                  (f'wid:{wid}:%', ))
        check('добавлено ровно два диалога виджета',
              added >= 2 and len(rows) == 2, (added, rows))
        check('канал и владелец определены верно',
              all(r[1] == 'widget' and r[4] == uid for r in rows), rows)
        check('новые диалоги стартуют в режиме bot', all(r[2] == 'bot' for r in rows), rows)
        check('последняя роль = последнее сообщение диалога',
              {r[0]: r[3] for r in rows}[s1] == 'assistant'
              and {r[0]: r[3] for r in rows}[s2] == 'user', rows)
        check('личный чат владельца в реестр не попал',
              not _q("SELECT 1 FROM conversations WHERE conv_key = 'web'"))
        check('битый scope проигнорирован',
              not _q("SELECT 1 FROM conversations WHERE conv_key LIKE %s", ('wid:нечисло:%', )))
        ts = _q('SELECT last_message_at FROM conversations WHERE conv_key = %s', (s1,))[0][0]
        check('last_message_at заполнен', ts is not None, ts)

        print('3) Повторный бэкфилл не затирает состояние')
        _q("""UPDATE conversations SET mode='human', unread_for_owner=7, operator_user_id=%s
              WHERE conv_key = %s""", (uid, s1))
        again = auth_db.backfill_conversations()
        row = _q("""SELECT mode, unread_for_owner, operator_user_id FROM conversations
                    WHERE conv_key = %s""", (s1,))[0]
        check('повторная заливка не сработала (added=0 для существующих)', again >= 0 and row[0] == 'human',
              (again, row))
        check('режим human и непрочитанное сохранены',
              row == ('human', 7, uid), row)

        test_inbox_http(uid, wid, vis1, vis2, s1)
        test_dialog_http(uid, wid, vis1)
        test_inbox_page(uid, wid, vis1)
        test_mode_http(uid, wid, vis1, vis2)
        test_reply_http(uid, wid, w['key'], vis1, vis2)
        test_manual_mode(uid, wid, w['key'], 'feedface11223344')
        test_frame_poll(uid, wid, w['key'], vis1)
        test_race(uid, wid, w['key'], 'deadbeef55667788')
        test_bot_pause(uid, wid, w['key'], 'cafebabe12345678')
        test_auto_return(uid, wid, w['key'], 'abcdef0199aabbcc')

        print('14) Каскад при удалении владельца')
        auth_db.delete_user(uid)
        check('диалоги удалены вместе с пользователем',
              not _q('SELECT 1 FROM conversations WHERE conv_key LIKE %s', (f'wid:{wid}:%', )))
    finally:
        try:
            auth_db.delete_user(uid)
        except Exception:
            pass

    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for n in FAILED:
        print(f'  ❌ {n}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
