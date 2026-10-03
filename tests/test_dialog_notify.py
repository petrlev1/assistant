# test_dialog_notify.py - оповещения о новых диалогах на e-mail (панель «Диалоги»)
#
# Что проверяется:
#   1) колонка users.notify_email и API панели (чтение, сохранение, права);
#   2) разбор адресов: запятая/точка с запятой, до трёх, проверка формата;
#   3) письмо уходит на первый вопрос нового собеседника — в виджете и в MAX,
#      в бот-режиме и в ручном;
#   4) повторные вопросы того же собеседника письма не шлют;
#   5) ограничение частоты: второй новый собеседник в те же 5 минут письма не даёт;
#   6) пустой адрес = оповещения выключены;
#   7) пробное письмо из панели (успех, отказ SMTP, пустой адрес).
#
# SMTP не нужен: отправка подменяется записью вызовов. Запуск из корня проекта:
#   venv/Scripts/python.exe tests/test_dialog_notify.py
import os
import sys
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
    name = 'testnotify_' + uuid.uuid4().hex[:8]
    ok, msg = auth_db.register_user(name, 'test-pass-123')
    if not ok:
        raise RuntimeError(f'не удалось создать тест-пользователя: {msg}')
    return _q('SELECT id FROM users WHERE username = %s', (name,))[0][0], name


class _FakeRAG:
    """Заглушка RAGCore: считает вызовы модели и отвечает предсказуемо."""

    def __init__(self):
        self.calls = 0
        self.settings = {}

    def ask_model(self, question, user_prompt=None, history=None):
        self.calls += 1
        return 'БОТ: ответ на «%s»' % question


def main():
    auth_db.init_db()
    auth_db.init_chat_history()
    auth_db.init_widgets()
    auth_db.init_max_channels()
    auth_db.init_conversations()

    import web_app, mailer

    print('1) Схема и API панели')
    cols = {r[0] for r in _q("""SELECT column_name FROM information_schema.columns
                               WHERE table_name = 'users'""")}
    check('в users есть колонка notify_email', 'notify_email' in cols, sorted(cols))

    uid, name = _make_test_user()
    print(f'Тестовый пользователь: {name} (id={uid})')
    client = web_app.app.test_client()
    try:
        check('без логина состояние не отдаётся', client.get('/api/inbox/notify').status_code == 401)
        check('без логина адреса не сохранить',
              client.post('/api/inbox/notify', json={'email': 'a@b.ru'}).status_code == 401)
        check('без логина пробное письмо не отправить',
              client.post('/api/inbox/notify/test', json={'email': 'a@b.ru'}).status_code == 401)

        with client.session_transaction() as sess:
            sess['user_id'] = uid
            sess['username'] = name

        state = client.get('/api/inbox/notify').get_json() or {}
        check('по умолчанию адресов нет, транспорт описан',
              state.get('email') == '' and state.get('emails') == []
              and isinstance(state.get('mail_ready'), bool), state)

        print('2) Разбор адресов и валидация')
        check('битый адрес — 400',
              client.post('/api/inbox/notify', json={'email': 'не-почта'}).status_code == 400)
        check('четыре адреса — 400',
              client.post('/api/inbox/notify',
                          json={'email': 'a@b.ru, c@d.ru, e@f.ru, g@h.ru'}).status_code == 400)

        r = client.post('/api/inbox/notify', json={'email': ' me@firma.ru ,  buh@firma.ru;dir@firma.ru '})
        body = r.get_json() or {}
        check('три адреса через запятую и точку с запятой принимаются',
              r.status_code == 200 and body.get('emails') == ['me@firma.ru', 'buh@firma.ru', 'dir@firma.ru'],
              body)
        check('строка сохранилась в БД (обрезаны только внешние пробелы)',
              auth_db.get_notify_email(uid) == 'me@firma.ru ,  buh@firma.ru;dir@firma.ru',
              auth_db.get_notify_email(uid))
        check('состояние панели отдаёт разобранные адреса',
              (client.get('/api/inbox/notify').get_json() or {}).get('emails')
              == ['me@firma.ru', 'buh@firma.ru', 'dir@firma.ru'], None)

        # Дальше письма шлём на один адрес — так проверки читаются проще
        client.post('/api/inbox/notify', json={'email': 'me@firma.ru'})

        print('3) Письмо о новом диалоге: виджет')
        ok, w = auth_db.create_widget(uid, 'notify-test')
        wid, wkey = w['id'], w['key']
        fake = _FakeRAG()
        letters = []
        real_rag_getter, real_ready = web_app.get_user_rag, web_app.rag_ready
        real_send_dialog = mailer.send_dialog_mail
        real_async = web_app._run_async
        web_app.get_user_rag = lambda user_id: fake
        web_app.rag_ready = True
        # Отправку делаем синхронной, а сам SMTP — записью вызова: проверки без сети
        web_app._run_async = lambda func, *args: func(*args)
        mailer.send_dialog_mail = lambda dialogs, recipients, **kw: (
            letters.append((dialogs, recipients)), (True, ''))[1]
        web_app._NOTIFY_LAST.clear()

        def ask(visitor, question, site='https://example.ru'):
            return client.post('/api/widget/ask', json={
                'key': wkey, 'visitor_id': visitor, 'site': site, 'question': question})

        try:
            r = ask('aaaabbbb1111cccc', 'Первый вопрос нового гостя')
            check('бот ответил новому гостю', r.status_code == 200, r.get_json())
            check('письмо о новом диалоге ушло на адрес из панели',
                  len(letters) == 1 and letters[0][1] == ['me@firma.ru'],
                  (len(letters), letters))
            dialog = (letters[0][0] or [{}])[0] if letters else {}
            check('в письме источник, вопрос и время',
                  'Виджет' in str(dialog.get('source')) and 'Первый вопрос' in str(dialog.get('question'))
                  and bool(dialog.get('time')), dialog)

            ask('aaaabbbb1111cccc', 'Второй вопрос того же гостя')
            check('второй вопрос того же собеседника письма не шлёт', len(letters) == 1, len(letters))

            ask('ffffeeee2222dddd', 'Вопрос другого гостя в те же 5 минут')
            check('ограничение частоты: второе письмо в те же 5 минут не уходит',
                  len(letters) == 1, len(letters))

            web_app._NOTIFY_LAST.clear()
            ask('1111222233334444', 'Гость после истечения интервала')
            check('после интервала письмо уходит снова', len(letters) == 2, len(letters))

            auth_db.set_notify_email(uid, '')
            ask('5555666677778888', 'Гость при выключенных оповещениях')
            check('пустой адрес = оповещения выключены', len(letters) == 2, len(letters))

            print('4) Письмо о новом диалоге: MAX, ручной режим')
            client.post('/api/inbox/notify', json={'email': 'me@firma.ru'})
            auth_db.save_max_channel(uid, 'notify-token', 999, 'Тест-бот', 'test_bot',
                                     'hookkey_notify_01', 'secret_notify_01')
            web_app._NOTIFY_LAST.clear()
            real_send = web_app._max_send
            web_app._max_send = lambda channel, text, peer: True
            try:
                channel = auth_db.get_max_channel(uid)
                peer = '999888777'
                client.post('/api/max/takeover', json={'visitor': peer})
                web_app._max_handle_update(channel, {
                    'update_type': 'message_created',
                    'message': {'sender': {'user_id': peer}, 'body': {'text': 'Вопрос при менеджере'}}})
                check('новый собеседник в MAX даёт письмо',
                      len(letters) == 3 and 'MAX' in str(letters[-1][0][0].get('source')),
                      (len(letters), letters[-1:] if letters else None))
                web_app._max_handle_update(channel, {
                    'update_type': 'message_created',
                    'message': {'sender': {'user_id': peer}, 'body': {'text': 'Ещё вопрос'}}})
                check('повторный вопрос в MAX письма не шлёт', len(letters) == 3, len(letters))
            finally:
                web_app._max_send = real_send
                auth_db.delete_max_channel(uid)

            print('5) Пробное письмо из панели')
            r = client.post('/api/inbox/notify/test', json={'email': 'me@firma.ru'})
            check('пробное письмо уходит и сообщает об успехе',
                  r.status_code == 200 and (r.get_json() or {}).get('success') is True, r.get_json())
            check('пробное письмо помечено как проверка',
                  letters and letters[-1][0][0].get('source') == 'Проверка оповещений', letters[-1:])

            mailer.send_dialog_mail = lambda dialogs, recipients, **kw: (False, 'SMTPAuthError: 535')
            r = client.post('/api/inbox/notify/test', json={'email': 'me@firma.ru'})
            check('отказ SMTP виден в панели (502 + текст ошибки)',
                  r.status_code == 502 and 'SMTPAuthError' in str((r.get_json() or {}).get('error')),
                  r.get_json())

            mailer.send_dialog_mail = real_send_dialog
            check('пробное письмо без адреса — 400',
                  client.post('/api/inbox/notify/test', json={'email': '   '}).status_code == 400)
        finally:
            web_app.get_user_rag, web_app.rag_ready = real_rag_getter, real_ready
            web_app._run_async = real_async
            web_app._NOTIFY_LAST.clear()
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
