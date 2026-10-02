# mailer.py - отправка писем по SMTP (заявки с сайта, служебные уведомления).
#
# Секретов в файлах нет: хост, логин и пароль ящика лежат в app_settings (PostgreSQL)
# и вводятся в админке (/admin → Настройки → Почта). Пустой пароль = отправка выключена.
#
# Ключи app_settings:
#   mail_enabled        (bool) - отправлять письма о заявках
#   mail_smtp_host      (str)  - например smtp.spaceweb.ru
#   mail_smtp_port      (int)  - 465 (SSL) или 587 (STARTTLS)
#   mail_smtp_mode      (str)  - ssl (465) | starttls (587) | plain (локальный релей)
#   mail_smtp_user      (str)  - логин ящика (mail@ragstone.ru)
#   mail_smtp_password  (str)  - пароль ящика
#   mail_from           (str)  - отправитель (пусто = логин)
#   mail_to             (str)  - получатель заявок
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

logger = logging.getLogger(__name__)

# Умолчания: почта домена ragstone.ru живёт на SpaceWeb (MX mx1/mx2.spaceweb.ru)
MAIL_DEFAULTS = {
    'mail_enabled': False,
    'mail_smtp_host': 'smtp.spaceweb.ru',
    'mail_smtp_port': 465,
    'mail_smtp_mode': 'ssl',
    'mail_smtp_user': 'mail@ragstone.ru',
    'mail_smtp_password': '',
    'mail_from': 'mail@ragstone.ru',
    'mail_to': 'mail@ragstone.ru',
}

MAIL_TIMEOUT = 20      # секунд на соединение и команды: заявка не должна держать поток


def mail_settings(settings=None):
    """Актуальные настройки почты: app_settings поверх умолчаний (недостающие ключи — из MAIL_DEFAULTS)."""
    if settings is None:
        try:
            from auth_db import get_all_settings
            settings = get_all_settings()
        except Exception as e:
            logger.error(f"Настройки почты: БД недоступна ({e}) — беру умолчания")
            settings = {}
    out = dict(MAIL_DEFAULTS)
    # Значение из БД перекрывает умолчание даже пустой строкой: админ может сознательно
    # очистить поле (например, убрать логин для локального релея). Нет ключа в БД — умолчание.
    for key in MAIL_DEFAULTS:
        value = (settings or {}).get(key, MAIL_DEFAULTS[key])
        out[key] = MAIL_DEFAULTS[key] if value is None else value
    try:
        out['mail_smtp_port'] = int(out['mail_smtp_port'])
    except (TypeError, ValueError):
        out['mail_smtp_port'] = MAIL_DEFAULTS['mail_smtp_port']
    out['mail_enabled'] = bool(out['mail_enabled'])
    mode = str(out['mail_smtp_mode'] or '').strip().lower()
    out['mail_smtp_mode'] = mode if mode in ('ssl', 'starttls', 'plain') else 'ssl'
    out['mail_from'] = str(out['mail_from'] or out['mail_smtp_user']).strip()
    out['mail_to'] = str(out['mail_to']).strip()
    out['mail_smtp_user'] = str(out['mail_smtp_user']).strip()
    out['mail_smtp_password'] = str(out['mail_smtp_password'])
    out['mail_smtp_host'] = str(out['mail_smtp_host']).strip()
    return out


def is_configured(settings=None):
    """Готово ли к отправке: есть хост, получатель и (если задан логин) пароль."""
    cfg = mail_settings(settings)
    if not cfg['mail_smtp_host'] or not cfg['mail_to']:
        return False
    if cfg['mail_smtp_user'] and not cfg['mail_smtp_password']:
        return False
    return True


def _lead_text(lead, cfg):
    """Тело письма простым текстом (копия для людей без HTML)."""
    lines = [
        "Новая заявка с сайта RAGSTONE", "",
        f"Имя:        {lead.get('name', '')}",
    ]
    for title, key in (("Телефон", "phone"), ("E-mail", "email"), ("Мессенджер", "messenger")):
        if lead.get(key):
            lines.append(f"{title + ':':11s} {lead[key]}")
    if lead.get('source'):
        lines.append(f"Источник:   {lead['source']}")
    if lead.get('page'):
        lines.append(f"Страница:   {lead['page']}")
    if lead.get('created_at'):
        lines.append(f"Время:      {lead['created_at']}")
    lines += ["", "Сообщение:", lead.get('message', '')]
    if lead.get('id'):
        lines += ["", f"Заявка №{lead['id']} в админке: /admin → Заявки"]
    return "\n".join(lines)


def _lead_html(lead, cfg):
    """Тело письма в HTML: те же поля, удобно читать с телефона."""
    rows = [("Имя", lead.get('name', ''))]
    for title, key in (("Телефон", "phone"), ("E-mail", "email"), ("Мессенджер", "messenger")):
        if lead.get(key):
            rows.append((title, lead[key]))
    if lead.get('source'):
        rows.append(("Источник", lead['source']))
    if lead.get('page'):
        rows.append(("Страница", lead['page']))
    if lead.get('created_at'):
        rows.append(("Время", lead['created_at']))
    tr = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#64748b">{html.escape(str(k))}</td>'
        f'<td style="padding:4px 0"><b>{html.escape(str(v))}</b></td></tr>' for k, v in rows)
    body = html.escape(str(lead.get('message', ''))).replace("\n", "<br>")
    footer = (f'<p style="color:#64748b;font-size:13px">Заявка №{lead["id"]} — '
              f'в админке: /admin → «Заявки»</p>') if lead.get('id') else ''
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a">'
        '<h2 style="margin:0 0 12px">Новая заявка с сайта RAGSTONE</h2>'
        f'<table style="border-collapse:collapse;font-size:14px">{tr}</table>'
        f'<p style="margin:16px 0 6px;color:#64748b">Сообщение:</p>'
        f'<div style="white-space:pre-wrap;border-left:3px solid #4f46e5;padding:8px 12px;'
        f'background:#f8fafc;font-size:14px">{body}</div>{footer}</div>')


def send_lead_mail(lead, settings=None):
    """Письмо о заявке на адрес из настроек. Возвращает (успех, текст ошибки)."""
    cfg = mail_settings(settings)
    if not is_configured(cfg):
        return False, "почта не настроена (нет хоста, получателя или пароля ящика)"
    if not cfg['mail_enabled']:
        return False, "отправка писем выключена в настройках"

    name = str(lead.get('name') or 'без имени')[:120]
    msg = EmailMessage()
    msg['Subject'] = f"RAGSTONE - заявка с сайта: {name}"
    msg['From'] = formataddr(("RAGSTONE заявки", cfg['mail_from']))
    msg['To'] = cfg['mail_to']
    if lead.get('email'):
        # Отвечать на письмо удобно прямо клиенту: Reply-To = его адрес
        msg['Reply-To'] = str(lead['email'])[:120]
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=(cfg['mail_from'].split('@')[-1] or 'ragstone.ru'))
    msg.set_content(_lead_text(lead, cfg))
    msg.add_alternative(_lead_html(lead, cfg), subtype='html')

    server = None
    try:
        context = ssl.create_default_context()
        mode = cfg['mail_smtp_mode']
        if mode == 'ssl':
            server = smtplib.SMTP_SSL(cfg['mail_smtp_host'], cfg['mail_smtp_port'],
                                      timeout=MAIL_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(cfg['mail_smtp_host'], cfg['mail_smtp_port'], timeout=MAIL_TIMEOUT)
            server.ehlo()
            if mode == 'starttls':
                server.starttls(context=context)
                server.ehlo()
        if cfg['mail_smtp_user']:
            server.login(cfg['mail_smtp_user'], cfg['mail_smtp_password'])
        server.send_message(msg)
        logger.info(f"📧 Заявка отправлена на {cfg['mail_to']} ({name})")
        return True, ""
    except Exception as e:
        # Пароль в лог не попадает: пишем только тип ошибки и адрес сервера
        err = f"{e.__class__.__name__}: {e}"
        logger.error(f"📧 Не удалось отправить заявку через {cfg['mail_smtp_host']}:{cfg['mail_smtp_port']} — {err}")
        return False, err[:500]
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def send_test_mail(settings=None):
    """Пробное письмо из админки: проверка хоста, логина и пароля до первой заявки."""
    return send_lead_mail({
        'name': 'Проверка настроек',
        'message': 'Тестовое письмо из админ-панели RAGSTONE. Если вы его видите — '
                   'форма заявок на сайте будет работать.',
        'source': 'Проверка из админки',
        'created_at': formatdate(localtime=True),
    }, settings=settings)
