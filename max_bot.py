# max_bot.py - Канал MAX (max.ru): тот же ассистент по базе знаний в мессенджере
"""Клиент MAX Bot API (https://dev.max.ru/docs-api) и фоновые воркеры канала.

Три особенности платформы определили устройство этого модуля:

1. Домен только platform-api2.max.ru, токен — ТОЛЬКО в заголовке Authorization
   (передача в query-параметрах отключена в 2026).
2. TLS-сертификат *.max.ru выдан УЦ Минцифры («Russian Trusted Sub CA»).
   Без корневого сертификата этого УЦ Python не подтвердит соединение, поэтому
   все запросы идут с verify=certs/russian_trusted_root_ca.pem.
3. Событие приходит вебхуком (POST /max/hook/<ключ>), а MAX ждёт ответ 200 в
   течение 30 секунд. Ответ ассистента рождается за секунды (LLM), поэтому
   обработчик вебхука только кладёт задачу в очередь, а считает её воркер.

Лимиты платформы: 2 сообщения/сек в один диалог, 30 rps суммарно,
text <= 4000 символов (длинные ответы режутся на части в send_text).
"""

import logging
import os
import queue
import threading
import time

import requests

logger = logging.getLogger(__name__)

API_BASE = os.environ.get('MAX_API_BASE', 'https://platform-api2.max.ru')
CA_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certs', 'russian_trusted_root_ca.pem')
TIMEOUT = 40                 # секунд на обычный запрос
TEXT_LIMIT = 4000            # лимит MAX на текст сообщения
SEND_INTERVAL = 0.6          # 2 сообщения/сек в диалог — держим запас
UPDATE_TYPES = ('message_created', 'bot_started')


def _verify():
    """Путь к CA Минцифры для *.max.ru; если файла нет — системное хранилище."""
    return CA_BUNDLE if os.path.exists(CA_BUNDLE) else True


def api(token, method, path, params=None, body=None, timeout=TIMEOUT):
    """Запрос к MAX API. Возвращает (ok, data); при ошибке data = {'error': ...}."""
    headers = {'Authorization': token or ''}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        resp = requests.request(method, API_BASE + path, params=params, json=body,
                                headers=headers, timeout=timeout, verify=_verify())
    except requests.exceptions.SSLError as exc:
        return False, {'error': 'TLS: %s' % exc}
    except requests.exceptions.RequestException as exc:
        return False, {'error': 'Сеть: %s' % exc}
    data = {}
    if resp.content:
        try:
            data = resp.json()
        except ValueError:
            data = {'raw': resp.text[:300]}
    if resp.status_code >= 400:
        msg = data.get('message') or data.get('error') if isinstance(data, dict) else ''
        return False, {'error': str(msg) or ('HTTP %s' % resp.status_code),
                       'status': resp.status_code, 'data': data}
    return True, (data if isinstance(data, dict) else {'data': data})


def get_me(token):
    """Карточка бота: user_id, name, username. Используется для проверки токена."""
    return api(token, 'GET', '/me')


def get_subscriptions(token):
    return api(token, 'GET', '/subscriptions')


def subscribe(token, url, secret, update_types=UPDATE_TYPES):
    """Подписка на вебхук: MAX будет слать события на url (HTTPS, порт 443)."""
    return api(token, 'POST', '/subscriptions',
               body={'url': url, 'secret': secret, 'update_types': list(update_types)})


def unsubscribe(token, url):
    return api(token, 'DELETE', '/subscriptions', params={'url': url})


def split_text(text, limit=TEXT_LIMIT):
    """Разбить длинный ответ на части по границам абзацев/слов (лимит MAX — 4000)."""
    text = (text or '').strip()
    if not text:
        return []
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind('\n', 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(' ', 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


# Частота отправки: 2 сообщения/сек в один диалог (иначе MAX ответит 429)
_send_lock = threading.Lock()
_send_last = {}


def _respect_rate(chat_key):
    with _send_lock:
        wait = (_send_last.get(chat_key, 0) + SEND_INTERVAL) - time.time()
    if wait > 0:
        time.sleep(wait)
    with _send_lock:
        _send_last[chat_key] = time.time()


def send_text(token, text, user_id=None, chat_id=None):
    """Отправить ответ собеседнику. Возвращает (ok, err)."""
    params = {'user_id': user_id} if user_id else {'chat_id': chat_id}
    for chunk in split_text(text):
        _respect_rate(str(user_id or chat_id))
        ok, data = api(token, 'POST', '/messages', params=params,
                       body={'text': chunk, 'notify': True})
        if not ok:
            return False, data.get('error') or 'не удалось отправить'
    return True, None


def get_updates(token, marker=None, timeout=30, limit=100, types=UPDATE_TYPES):
    """Long polling (запасной транспорт). Возвращает (ok, updates, marker)."""
    params = {'timeout': timeout, 'limit': limit, 'types': ','.join(types)}
    if marker is not None:
        params['marker'] = marker
    ok, data = api(token, 'GET', '/updates', params=params, timeout=timeout + 15)
    if not ok:
        return False, [], data
    return True, (data.get('updates') or []), data.get('marker')


# Дедупликация: MAX повторяет доставку вебхука до 10 раз (экспоненциально, до 8 ч),
# а long polling может вернуть то же событие после перезапуска.
_seen_lock = threading.Lock()
_seen = {}
_SEEN_TTL = 600


def is_duplicate(update):
    """True, если такое событие уже обрабатывали за последние 10 минут."""
    message = update.get('message') or {}
    recipient = message.get('recipient') or {}
    key = '%s:%s:%s' % (recipient.get('user_id') or recipient.get('chat_id'),
                        (message.get('sender') or {}).get('user_id'),
                        message.get('timestamp'))
    if key.endswith('None'):          # нечего сравнивать — не считаем дублем
        return False
    now = time.time()
    with _seen_lock:
        for old in [k for k, t in _seen.items() if now - t > _SEEN_TTL]:
            _seen.pop(old, None)
        if key in _seen:
            return True
        _seen[key] = now
        return False


class UpdateQueue:
    """Очередь событий: вебхук кладёт задачу и сразу отвечает 200, отвечают воркеры."""

    def __init__(self, handler, workers=3):
        self._handler = handler
        self._queue = queue.Queue()
        self._threads = []
        self._n = max(1, int(workers))

    def start(self):
        if self._threads:
            return
        for i in range(self._n):
            thread = threading.Thread(target=self._loop, name='max-worker-%d' % i, daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info('MAX: воркеры очереди запущены (%d)', self._n)

    def put(self, channel, update):
        self._queue.put((channel, update))

    def _loop(self):
        while True:
            channel, update = self._queue.get()
            try:
                if not is_duplicate(update):
                    self._handler(channel, update)
            except Exception:
                logger.exception('MAX: ошибка обработки события')
            finally:
                self._queue.task_done()


class PollingManager:
    """Long polling — запасной транспорт для каналов, которым MAX не принял вебхук.

    Документация MAX рекомендует вебхук для продуктовой эксплуатации, поэтому
    опрос включается только там, где вебхук не поднялся (mode='polling').
    """

    def __init__(self, handler):
        self._handler = handler
        self._lock = threading.Lock()
        self._pollers = {}

    def ensure(self, channel):
        with self._lock:
            if channel.get('id') in self._pollers:
                return
            stop = threading.Event()
            self._pollers[channel['id']] = stop
        thread = threading.Thread(target=self._loop, args=(channel, stop),
                                  name='max-poll-%s' % channel.get('id'), daemon=True)
        thread.start()
        logger.info('MAX: long polling запущен для канала #%s', channel.get('id'))

    def stop(self, channel_id):
        with self._lock:
            stop = self._pollers.pop(channel_id, None)
        if stop:
            stop.set()

    def stop_all(self):
        with self._lock:
            entries = list(self._pollers.items())
            self._pollers = {}
        for _, stop in entries:
            stop.set()

    def _loop(self, channel, stop):
        marker = None
        while not stop.is_set():
            ok, updates, new_marker = get_updates(channel.get('token') or '', marker=marker, timeout=30)
            if not ok:
                stop.wait(15)
                continue
            if new_marker is not None:
                marker = new_marker
            for update in updates:
                if stop.is_set():
                    return
                try:
                    self._handler(channel, update)
                except Exception:
                    logger.exception('MAX: ошибка обработки события (long polling)')
