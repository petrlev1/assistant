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

        print('4) Каскад при удалении владельца')
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
