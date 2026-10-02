# test_lead_form.py - дымовой тест формы заявок (модальное окно на /prices и лендинге)
#
# Что проверяется:
#   1) БД: таблица leads создаётся init_leads(), заявка сохраняется со всеми полями;
#   2) валидация POST /api/lead: имя, сообщение, ЛЮБОЙ ОДИН контакт; формат e-mail и телефона;
#   3) honeypot-поле company: боту отвечаем «успех», но в БД ничего не пишем;
#   4) лимит 5 заявок в час с одного IP;
#   5) письмо: без настроек заявка сохраняется с mail_sent = False, а с настроенным SMTP
#      письмо реально уходит (проверяется на локальном фейковом SMTP-сервере, без сети);
#   6) админка: блоки почты и заявок доступны только с админ-сессией;
#   7) страницы /prices и / отдают форму (data-lead-open) и больше не содержат mailto заявок.
#
# Внешняя сеть и LLM не нужны. Нужна живая PostgreSQL (как у остальных тестов проекта).
# Запуск из корня проекта:  venv/Scripts/python.exe tests/test_lead_form.py
import base64
import email
import json
from email.header import decode_header, make_header
import os
import socket
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import auth_db
import mailer
import web_app

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    mark = 'OK  ' if cond else 'FAIL'
    line = f'  [{mark}] {name}'
    if not cond and extra:
        line += f'  → {extra}'
    print(line)


MARK = '__lead_test__'


def post(client, payload, ip='10.9.9.1'):
    return client.post('/api/lead', json=payload, environ_base={'REMOTE_ADDR': ip})


def db_rows(sql, params=()):
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def cleanup():
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM leads WHERE source LIKE %s OR name LIKE %s", (MARK + '%', MARK + '%'))
    conn.commit()
    conn.close()


# === Фейковый SMTP-сервер: принимает письмо и складывает его в FAKE.messages ===
class FakeSMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 fake ESMTP ready\r\n')
        while True:
            line = self.rfile.readline()
            if not line:
                break
            cmd = line.decode('utf-8', 'replace').strip()
            up = cmd.upper()
            if up.startswith(('EHLO', 'HELO')):
                self.wfile.write(b'250-fake\r\n250 SIZE 10240000\r\n')
            elif up.startswith(('MAIL FROM', 'RCPT TO', 'RSET')):
                self.wfile.write(b'250 OK\r\n')
            elif up.startswith('DATA'):
                self.wfile.write(b'354 End data with <CR><LF>.<CR><LF>\r\n')
                buf = []
                while True:
                    dl = self.rfile.readline()
                    if not dl or dl.strip() == b'.':
                        break
                    buf.append(dl)
                FAKE.msgs.append(b''.join(buf).decode('utf-8', 'replace'))
                self.wfile.write(b'250 OK queued\r\n')
            elif up.startswith('QUIT'):
                self.wfile.write(b'221 Bye\r\n')
                break
            elif up.startswith('NOOP'):
                self.wfile.write(b'250 OK\r\n')
            else:
                self.wfile.write(b'250 OK\r\n')


class FAKE:
    msgs = []


def start_fake_smtp():
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), FakeSMTPHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def decode_message(raw):
    msg = email.message_from_string(raw)
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() == 'text':
            payload = part.get_payload(decode=True) or b''
            parts.append(payload.decode(part.get_content_charset() or 'utf-8', 'replace'))
    return msg, "\n".join(parts)


def main():
    ok_db = auth_db.init_leads()
    check('init_leads() создаёт таблицу leads', ok_db)
    rows = db_rows("SELECT column_name FROM information_schema.columns "
                   "WHERE table_name = 'leads' ORDER BY column_name")
    cols = {r[0] for r in rows}
    check('в leads есть все поля заявки',
          {'id', 'name', 'phone', 'email', 'messenger', 'message', 'source', 'page', 'ip',
           'user_agent', 'mail_sent', 'mail_error', 'created_at'} <= cols,
          str(sorted(cols)))

    cleanup()
    client = web_app.app.test_client()

    # --- валидация ---
    base = {'name': MARK + ' Иван', 'message': 'Нужна установка на своём сервере'}
    r = post(client, dict(base, phone='+7 999 123-45-67'), ip='10.9.9.2')
    check('заявка с телефоном принята (200)', r.status_code == 200, r.get_data(as_text=True)[:200])

    r = post(client, {'message': 'без имени', 'phone': '+79991234567'}, ip='10.9.9.3')
    check('без имени → 400', r.status_code == 400, str(r.status_code))

    r = post(client, {'name': MARK, 'phone': '+79991234567'}, ip='10.9.9.4')
    check('без сообщения → 400', r.status_code == 400, str(r.status_code))

    r = post(client, {'name': MARK, 'message': 'нет ни одного контакта'}, ip='10.9.9.5')
    check('без контактов → 400 и понятный текст',
          r.status_code == 400 and 'контакт' in (r.get_json() or {}).get('error', ''),
          str(r.get_json()))

    r = post(client, {'name': MARK, 'message': 'кривой адрес', 'email': 'ivan@mail'}, ip='10.9.9.6')
    check('некорректный e-mail → 400', r.status_code == 400, str(r.status_code))

    r = post(client, {'name': MARK, 'message': 'короткий телефон', 'phone': '123'}, ip='10.9.9.7')
    check('телефон из 3 цифр → 400', r.status_code == 400, str(r.status_code))

    r = post(client, {'name': MARK, 'message': 'только мессенджер', 'messenger': '@petr'}, ip='10.9.9.8')
    check('заявка только с мессенджером принята', r.status_code == 200, str(r.status_code))

    # --- запись в БД ---
    payload = {'name': MARK + ' Пётр', 'message': 'Облако на 20 человек',
               'email': 'petr@example.ru', 'phone': '+7 999 000-11-22',
               'source': MARK + 'Тарифы → Индивидуальная установка', 'page': '/prices'}
    r = post(client, payload, ip='10.9.9.9')
    lead_id = (r.get_json() or {}).get('lead_id')
    check('валидная заявка → 200 и id заявки', r.status_code == 200 and bool(lead_id), str(r.get_json()))
    row = db_rows("SELECT name, phone, email, source, page, ip, mail_sent, mail_error "
                  "FROM leads WHERE id = %s", (lead_id,))
    check('заявка в БД со всеми полями', bool(row) and row[0][0] == payload['name']
          and row[0][3] == payload['source'] and row[0][4] == '/prices' and row[0][5] == '10.9.9.9',
          str(row))
    check('письмо не настроено → mail_sent = False и причина записана',
          row and row[0][6] is False and bool(row[0][7]), str(row[0][6:8]))

    # --- honeypot ---
    before = db_rows("SELECT COUNT(*) FROM leads")[0][0]
    r = post(client, dict(base, phone='+79991234567', company='ООО Бот'), ip='10.9.9.10')
    after = db_rows("SELECT COUNT(*) FROM leads")[0][0]
    check('honeypot: боту отвечаем успехом, но заявка не пишется',
          r.status_code == 200 and (r.get_json() or {}).get('spam') and before == after,
          f'{r.status_code} {r.get_json()} {before}->{after}')

    # --- лимиты: квота заявок на IP + отдельный (более свободный) лимит попыток ---
    cap = web_app._LEAD_SAVED_LIMIT[0]
    codes = [post(client, dict(base, phone='+79990000000'), ip='10.9.9.77').status_code
             for _ in range(cap + 1)]
    check(f'лимит заявок: {cap + 1}-я заявка с одного IP → 429',
          codes[:cap] == [200] * cap and codes[cap] == 429, str(codes))

    ip_err = '10.9.9.78'
    fails = [post(client, {'name': MARK, 'message': 'без контактов'}, ip=ip_err).status_code
             for _ in range(6)]
    r = post(client, dict(base, phone='+79990002222'), ip=ip_err)
    check('отказы валидации не съедают квоту: после 6 ошибок заявка проходит',
          fails == [400] * 6 and r.status_code == 200, f'{fails} -> {r.status_code}')

    # --- письмо через фейковый SMTP ---
    server, port = start_fake_smtp()
    old_settings = {}
    keys = {'mail_enabled': True, 'mail_smtp_host': '127.0.0.1', 'mail_smtp_port': port,
            'mail_smtp_mode': 'plain', 'mail_smtp_user': '', 'mail_smtp_password': '',
            'mail_from': 'mail@ragstone.ru', 'mail_to': 'mail@ragstone.ru'}
    current = auth_db.get_all_settings()
    for key in keys:
        old_settings[key] = current.get(key)
    auth_db.set_settings(keys)
    try:
        cfg = mailer.mail_settings()
        check('настройки почты читаются из app_settings',
              cfg['mail_smtp_host'] == '127.0.0.1' and cfg['mail_smtp_port'] == port
              and cfg['mail_enabled'] is True and cfg['mail_smtp_mode'] == 'plain', str(cfg))
        check('мусорный режим шифрования откатывается к SSL',
              mailer.mail_settings({'mail_smtp_mode': 'RC4'})['mail_smtp_mode'] == 'ssl')
        ok, err = mailer.send_lead_mail({'name': MARK + ' Тест', 'message': 'Проверка письма',
                                         'phone': '+79990001122', 'source': MARK + 'проверка',
                                         'id': 999999, 'created_at': '02.10.2026 16:00'})
        check('send_lead_mail() отдаёт успех на живом SMTP', ok, err)
        check('фейковый SMTP получил письмо', len(FAKE.msgs) == 1, str(len(FAKE.msgs)))
        if FAKE.msgs:
            msg, body = decode_message(FAKE.msgs[0])
            # Тема с кириллицей приходит в MIME-кодировке (=?utf-8?b?…?=) — раскодируем
            subject = str(make_header(decode_header(msg.get('Subject') or '')))
            check('в письме есть тема с именем заявителя',
                  'заявка с сайта' in subject.lower() and MARK + ' Тест' in subject, subject)
            check('письмо ушло на адрес из настроек', msg.get('To') == 'mail@ragstone.ru', str(msg.get('To')))
            check('в письме есть текст заявки и контакты',
                  'Проверка письма' in body and '+79990001122' in body, body[:200])

        # реальная заявка через HTTP: письмо уходит и отметка в БД становится True
        r = post(client, {'name': MARK + ' Письмо', 'message': 'Заявка с письмом',
                          'email': 'mail-lead@example.ru'}, ip='10.9.9.88')
        lead2 = (r.get_json() or {}).get('lead_id')
        check('HTTP-заявка отдаёт mail_sent = True при настроенном SMTP',
              r.status_code == 200 and (r.get_json() or {}).get('mail_sent') is True, str(r.get_json()))
        sent_row = db_rows("SELECT mail_sent FROM leads WHERE id = %s", (lead2,))
        check('в БД отметка об отправленном письме', sent_row and sent_row[0][0] is True, str(sent_row))
    finally:
        server.shutdown()
        server.server_close()
        restore = {k: v for k, v in old_settings.items() if v is not None}
        auth_db.set_settings(restore) if restore else None
        for k, v in old_settings.items():
            if v is None:
                conn = auth_db.get_db_connection()
                cur = conn.cursor()
                cur.execute("DELETE FROM app_settings WHERE key = %s", (k,))
                conn.commit()
                conn.close()

    # --- админка: доступ только с админ-сессией ---
    anon = web_app.app.test_client()
    r = anon.post('/admin/api/mail-settings', json=keys)
    check('сохранение настроек почты без админа → 403', r.status_code == 403, str(r.status_code))
    r = anon.post('/admin/api/lead-delete', json={'lead_id': 1})
    check('удаление заявки без админа → 403', r.status_code == 403, str(r.status_code))
    r = anon.get('/admin')
    check('страница /admin без сессии — форма входа (200)', r.status_code == 200,
          str(r.status_code))

    admin = web_app.app.test_client()
    with admin.session_transaction() as sess:
        sess['admin'] = True
    r = admin.get('/admin')
    html = r.get_data(as_text=True)
    check('админка отдаёт вкладку «Заявки» и блок «Почта»',
          r.status_code == 200 and 'data-tab="leads"' in html and 'mail_smtp_host' in html, str(r.status_code))
    check('в админке видны заявки теста', MARK in html)
    bad = admin.post('/admin/api/mail-settings', json={'mail_smtp_host': 'smtp.spaceweb.ru',
                                                       'mail_smtp_port': 465, 'mail_from': 'не-адрес',
                                                       'mail_to': 'mail@ragstone.ru'})
    check('проверка адресов в настройках почты (400)', bad.status_code == 400, str(bad.status_code))
    del_id = db_rows("SELECT id FROM leads WHERE name LIKE %s LIMIT 1", (MARK + '%',))
    if del_id:
        r = admin.post('/admin/api/lead-delete', json={'lead_id': del_id[0][0]})
        check('админ удаляет заявку', r.status_code == 200 and r.get_json().get('success'), str(r.get_json()))

    # --- публичные страницы ---
    for url in ('/prices', '/tariffs', '/'):
        page = web_app.app.test_client().get(url).get_data(as_text=True)
        check(f'{url} отдаёт форму заявки (кнопка + модальное окно)',
              'data-lead-open' in page and 'leadForm' in page and 'lead_form.js' in page, url)
        check(f'{url} больше не отправляет на mailto с тарифами',
              'mailto:mail@ragstone.ru?subject' not in page, url)

    cleanup()
    print()
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    if FAILED:
        print('Проваленные проверки:')
        for name in FAILED:
            print('  -', name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
