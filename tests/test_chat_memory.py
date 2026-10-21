# test_chat_memory.py - дымовой тест памяти диалога (вариант B)
#
# Что проверяется:
#   1) помощники истории: дубль текущего вопроса, вырезание блока «Источники:»,
#      склейка прошлых вопросов в поисковый запрос;
#   2) сборка messages в RAGCore.ask_model: системный промт + история + текущий вопрос,
#      alt_query уходит в поиск по БЗ, точные пути (прайс, «вопрос-ответ») пробуют склейку;
#   3) слой БД: своя область device_id (без наследственной 'web'), TTL, бюджет символов,
#      граница «Новый диалог» (chat_sessions);
#   4) HTTP-поток через test_client: /ask передаёт историю, /api/chat/new её обнуляет,
#      виджет не видит историю владельца, настройки chat_memory_* выключают память.
#
# Сети и LLM не требуется: get_user_rag и requests.post подменяются заглушками.
# Запуск из корня проекта:  venv/Scripts/python.exe tests/test_chat_memory.py   (Linux-сервер: venv/bin/python tests/test_chat_memory.py)
import sys
import time
import uuid

import os

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:                                    # Windows-консоль: русский вывод без падений
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import auth_db
import rag_core
import web_app

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    mark = 'OK  ' if cond else 'FAIL'
    line = f'  [{mark}] {name}'
    if not cond and extra != '':
        line += f'  -> {extra!r}'
    print(line)


# === 1) Помощники истории (без БД) ==================================

def test_history_helpers():
    print('\n1) Помощники истории диалога')
    history = [
        {'role': 'user', 'message': 'Сколько стоит насос X?'},
        {'role': 'assistant', 'message': '12 500 ₽\n\nИсточники:\n- price.csv'},
        {'role': 'user', 'message': 'а у него какая мощность?'},
        {'role': 'assistant', 'message': '2 кВт'},
        {'role': 'user', 'message': 'а второй?'},          # текущий вопрос (уже в БД)
    ]
    prepared = rag_core._prepare_chat_history(history, 'а второй?')
    check('дубль текущего вопроса убран', len(prepared) == 4, prepared)
    check('последнее сообщение контекста — ответ бота',
          prepared[-1] == {'role': 'assistant', 'message': '2 кВт'}, prepared[-1])
    check('блок «Источники:» вырезан из ответа бота',
          prepared[1]['message'] == '12 500 ₽', prepared[1]['message'])
    check('роли user/assistant сохранены',
          [h['role'] for h in prepared] == ['user', 'assistant', 'user', 'assistant'],
          [h['role'] for h in prepared])

    q = rag_core._context_search_query(prepared, 'а второй?')
    check('поисковый запрос = прошлые вопросы + текущий',
          q == 'Сколько стоит насос X? а у него какая мощность? а второй?', q)
    q1 = rag_core._context_search_query(prepared, 'а второй?', max_questions=1)
    check('chat_memory_context_questions=1 берёт только последний вопрос',
          q1 == 'а у него какая мощность? а второй?', q1)
    check('без истории поисковый запрос = None',
          rag_core._context_search_query([], 'привет') is None)


# === 2) Сборка messages в RAGCore.ask_model =========================

class _Settings:
    """Заглушка RAGSettings: «БД» (db) + снимок (data) + get/reload.

    Настоящий RAGSettings держит снимок настроек и обновляет его только в reload(),
    поэтому заглушка повторяет это: правки в db видны лишь после reload(). На этом
    расхождении ловится метка чат-лога, отставшая на одну смену настроек.
    """

    def __init__(self, **overrides):
        self.db = {
            'disable_llm_models': False,
            'disable_qa_corrections': False,
            'disable_knowledge_base_search': False,
            'disable_hybrid_search': False,
            'search_top_k': 5,
            'search_alpha': 0.7,
            'relevance_threshold': 0.2,
            'max_context_fragments': 100,
            'chat_memory_context_questions': 2,
            'llm_model': 'test-model',
            'llm_base_url': 'http://127.0.0.1:9/v1',
        }
        self.db.update(overrides)
        self.data = dict(self.db)

    def reload(self):
        self.data = dict(self.db)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return 'test-key'


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {'choices': [{'message': {'content': 'ОТВЕТ МОДЕЛИ'}}]}


class _PostRecorder:
    """Подмена requests.post: записывает payload, отдаёт фальшивый ответ."""

    def __init__(self):
        self.payloads = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json or {})
        return _FakeResponse()


def _stub_rag(**settings):
    """RAGCore без конструктора (БЗ/эмбеддинги/сеть не поднимаем)."""
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.settings = _Settings(**settings)
    rag.current_user_id = 1
    rag._setup_client = lambda: None
    rag._sanitize_text = lambda text: text
    rag._try_price_answer = lambda q: None
    rag._find_qa_correction = lambda q: None
    rag.searches = []
    rag.price_queries = []

    def find_relevant_info(query, top_k=None, alpha=None, alt_query=None):
        rag.searches.append({'query': query, 'alt_query': alt_query})
        return 'КОНТЕКСТ БАЗЫ', ['doc.txt']

    rag.find_relevant_info = find_relevant_info
    return rag


def test_ask_model_messages():
    print('\n2) ask_model: сообщения и поисковый запрос')
    post = _PostRecorder()
    rag_core.requests.post = post

    rag = _stub_rag()
    history = [
        {'role': 'user', 'message': 'Сколько стоит насос X?'},
        {'role': 'assistant', 'message': '12 500 ₽'},
    ]
    answer = rag.ask_model('а второй?', user_prompt='Ты — ассистент.', history=history)
    check('ответ модели получен', answer.startswith('ОТВЕТ МОДЕЛИ'), answer)
    msgs = post.payloads[-1]['messages']
    check('в messages есть системный промт, история и текущий вопрос',
          [m['role'] for m in msgs] == ['system', 'user', 'assistant', 'user'],
          [m['role'] for m in msgs])
    check('текущий вопрос последний',
          msgs[-1]['content'] == 'а второй?', msgs[-1])
    check('история ушла модели как есть',
          msgs[1]['content'] == 'Сколько стоит насос X?' and msgs[2]['content'] == '12 500 ₽',
          msgs[1:3])
    check('контекст БЗ попал в системный промт',
          'КОНТЕКСТ БАЗЫ' in msgs[0]['content'], msgs[0]['content'][:120])
    check('поиск по БЗ шёл с осмысленным alt_query',
          rag.searches[-1]['alt_query'] == 'Сколько стоит насос X? а второй?', rag.searches[-1])

    # Без истории поведение прежнее: 2 сообщения, alt_query отсутствует
    rag2 = _stub_rag()
    rag2.ask_model('сколько стоит насос X?', user_prompt=None)
    msgs2 = post.payloads[-1]['messages']
    check('без истории messages = системный промт + вопрос',
          [m['role'] for m in msgs2] == ['system', 'user'], [m['role'] for m in msgs2])
    check('без истории alt_query = None',
          rag2.searches[-1]['alt_query'] is None, rag2.searches[-1])

    # Точный ценовой путь: сначала точный вопрос, затем склейка с прошлым
    rag3 = _stub_rag()
    seen = []

    def price(q):
        seen.append(q)
        return 'ЦЕНА ИЗ ПРАЙСА' if q == 'Сколько стоит насос X? а второй?' else None

    rag3._try_price_answer = price
    out = rag3.ask_model('а второй?', user_prompt=None,
                         history=[{'role': 'user', 'message': 'Сколько стоит насос X?'}])
    check('прайс сначала пробует точный вопрос', seen and seen[0] == 'а второй?', seen)
    check('прайс на follow-up отвечает по склейке с прошлым вопросом',
          out == 'ЦЕНА ИЗ ПРАЙСА' and len(seen) == 2, (out, seen))

    # Исправление «вопрос-ответ»: склейка истории его НЕ подтягивает (только текущий вопрос)
    rag4 = _stub_rag()
    corr_seen = []

    def find_qa(q):
        corr_seen.append(q)
        return {'question': 'мощность насоса X?', 'answer': 'ИСПРАВЛЕННЫЙ ОТВЕТ',
                'score': 0.99, 'exact': False} if q.strip() == 'мощность насоса X?' else None

    rag4._find_qa_correction = find_qa
    out4 = rag4.ask_model('а второй?', user_prompt=None,
                          history=[{'role': 'user', 'message': 'мощность насоса X?'}])
    check('исправление ищется только по текущему вопросу, не по склейке истории',
          corr_seen == ['а второй?'], corr_seen)
    check('чужое исправление не подменяет ответ на другой вопрос',
          out4.startswith('ОТВЕТ МОДЕЛИ'), out4)


def test_qa_corrections():
    """Исправление «✏️»: точный вопрос — дословно, похожий — проверенный факт в контексте."""
    print('\n2b) Исправления «вопрос-ответ»: точное / похожее совпадение')
    post = _PostRecorder()
    rag_core.requests.post = post
    hit = {'question': 'Мембранный элемент OV-ULP 2012-100 (100 галлонов в мин.) '
                       'Для Аквафор осмо - 50 (100) подходит?',
           'answer': 'Да, мембранный элемент OV-ULP 2012-100 подходит для Аквафор Осмо',
           'score': 0.979, 'exact': False}

    # 1) точное совпадение вопроса → готовый ответ дословно, без LLM
    rag = _stub_rag()
    rag._find_qa_correction = lambda q: dict(hit, exact=True)
    before = len(post.payloads)
    out = rag.ask_model(hit['question'], user_prompt=None)
    check('точный вопрос: исправление дословно, без вызова LLM',
          out.startswith('Да, мембранный элемент') and len(post.payloads) == before, out)
    check('точный вопрос: источник newdatabase.csv в ответе', 'newdatabase.csv' in out, out)

    # 2) тот же артикул, другой смысл («- описание») → отвечает модель, факт — в контексте
    rag2 = _stub_rag()
    rag2._find_qa_correction = lambda q: dict(hit)
    out2 = rag2.ask_model(hit['question'] + ' - описание.', user_prompt='Ты — ассистент.')
    sysmsg = post.payloads[-1]['messages'][0]['content']
    check('похожий вопрос: отвечает модель, а не готовый текст',
          out2.startswith('ОТВЕТ МОДЕЛИ'), out2)
    check('похожий вопрос: проверенный ответ идёт первым в контексте',
          'ПРОВЕРЕННЫЙ ОТВЕТ' in sysmsg and sysmsg.index('ПРОВЕРЕННЫЙ ОТВЕТ') < sysmsg.index('КОНТЕКСТ БАЗЫ'),
          sysmsg[:300])
    check('похожий вопрос: в промте правило про «вопрос о другом»',
          'если вопрос о другом' in sysmsg.lower(), sysmsg[:400])
    check('похожий вопрос: источник newdatabase.csv показан', 'newdatabase.csv' in out2, out2)

    # 3) режим "answer" — прежнее поведение (дословно на любой похожий вопрос)
    rag3 = _stub_rag(qa_correction_fuzzy_mode='answer')
    rag3._find_qa_correction = lambda q: dict(hit)
    out3 = rag3.ask_model(hit['question'] + ' - описание.', user_prompt=None)
    check('режим answer: дословная выдача сохранена',
          out3.startswith('Да, мембранный элемент'), out3)

    # 4) дубль пары из результатов поиска вырезается из контекста
    rag5 = _stub_rag()
    rag5._find_qa_correction = lambda q: dict(hit)
    dup = 'Вопрос — ' + hit['question'] + ', Ответ — ' + hit['answer'] + '.'
    rag5.find_relevant_info = lambda query, top_k=None, alpha=None, alt_query=None: (
        dup + '\nДРУГОЙ ФРАГМЕНТ БАЗЫ', ['doc.txt'])
    rag5.ask_model(hit['question'] + ' - описание.', user_prompt=None)
    sysmsg5 = post.payloads[-1]['messages'][0]['content']
    check('строка newdatabase.csv не дублируется в контексте',
          sysmsg5.count('Да, мембранный элемент') == 1 and 'ДРУГОЙ ФРАГМЕНТ БАЗЫ' in sysmsg5,
          sysmsg5.count('Да, мембранный элемент'))


# === 3) Слой БД ====================================================

def _make_test_user():
    """Тестовый пользователь в локальной БД (удаляется в конце)."""
    name = 'testmem_' + uuid.uuid4().hex[:8]
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


def test_llm_payload():
    """payload запроса: qwen3 на DashScope просят не «думать», остальным поле не шлём."""
    print('\n2c) payload к LLM: enable_thinking и каталог моделей')
    post = _PostRecorder()
    rag_core.requests.post = post

    rag = _stub_rag(llm_provider='DashScope', llm_model='qwen3.7-flash',
                    llm_base_url='https://dashscope-intl.aliyuncs.com/compatible-mode/v1')
    rag.ask_model('вопрос', user_prompt=None)
    check('DashScope + qwen3.7-flash: enable_thinking=False',
          post.payloads[-1].get('enable_thinking') is False, list(post.payloads[-1]))

    rag = _stub_rag(llm_provider='DashScope', llm_model='qwen-plus',
                    llm_base_url='https://dashscope-intl.aliyuncs.com/compatible-mode/v1')
    rag.ask_model('вопрос', user_prompt=None)
    check('DashScope + не-qwen3 модель: поле не отправляем',
          'enable_thinking' not in post.payloads[-1], list(post.payloads[-1]))

    rag = _stub_rag(llm_provider='Local (llama.cpp)', llm_model='qwen3-4b-instruct-2507',
                    llm_base_url='http://85.234.31.16:8443/v1')
    rag.ask_model('вопрос', user_prompt=None)
    check('локальный сервер: поле не отправляем',
          'enable_thinking' not in post.payloads[-1], list(post.payloads[-1]))

    import model_catalog
    check('каталог DashScope: одна рабочая модель — qwen3.7-flash',
          model_catalog.PROVIDERS['DashScope']['models'] == ['qwen3.7-flash'],
          model_catalog.PROVIDERS['DashScope']['models'])


def test_db_layer(uid):
    print('\n3) Слой БД: области, TTL, лимиты, граница диалога')
    scope = 'test:' + uuid.uuid4().hex[:10]
    auth_db.save_message(uid, 'user', 'вопрос про насос', device_id=scope)
    auth_db.save_message(uid, 'assistant', 'ответ 12500', device_id=scope)

    ctx = auth_db.get_prompt_context(uid, scope, limit=20, max_chars=4000, ttl_minutes=120)
    check('своя область device_id отдаётся в контекст',
          [c['message'] for c in ctx] == ['вопрос про насос', 'ответ 12500'], ctx)

    other = auth_db.get_prompt_context(uid, 'test:other', limit=20, max_chars=4000, ttl_minutes=120)
    check('чужая область device_id пуста (нет утечки диалога)', other == [], other)

    # «Новый диалог» сдвигает границу чтения контекста
    auth_db.start_new_chat_session(uid, scope)
    check('после «Нового диалога» контекст пуст',
          auth_db.get_prompt_context(uid, scope) == [],
          auth_db.get_prompt_context(uid, scope))
    check('«Новый диалог» не удаляет историю (лента осталась)',
          len(auth_db.get_history(uid, device_id=scope)) == 2,
          auth_db.get_history(uid, device_id=scope))
    auth_db.save_message(uid, 'user', 'новый вопрос после метки', device_id=scope)
    check('в контекст попадает только то, что после метки',
          [c['message'] for c in auth_db.get_prompt_context(uid, scope)] == ['новый вопрос после метки'],
          auth_db.get_prompt_context(uid, scope))

    # TTL: старое сообщение не тянется в промпт, пока есть свежее
    scope_ttl = 'test:' + uuid.uuid4().hex[:10]
    conn = auth_db.get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_history (user_id, role, message, device_id, created_at) "
        "VALUES (%s, 'user', 'древний вопрос', %s, NOW() - INTERVAL '3 hours')",
        (uid, scope_ttl))
    conn.commit()
    cur.close()
    conn.close()
    auth_db.save_message(uid, 'user', 'свежий вопрос', device_id=scope_ttl)
    with_ttl = auth_db.get_prompt_context(uid, scope_ttl, ttl_minutes=120)
    check('TTL: переписка старше 120 минут не уходит в промпт',
          [c['message'] for c in with_ttl] == ['свежий вопрос'], with_ttl)
    no_ttl = auth_db.get_prompt_context(uid, scope_ttl, ttl_minutes=0)
    check('TTL=0: история не разрывается по времени (оба сообщения)',
          [c['message'] for c in no_ttl] == ['древний вопрос', 'свежий вопрос'], no_ttl)

    # Бюджет символов: старое отбрасывается первым
    scope_chars = 'test:' + uuid.uuid4().hex[:10]
    auth_db.save_message(uid, 'user', 'С' * 80, device_id=scope_chars)
    auth_db.save_message(uid, 'assistant', 'О' * 80, device_id=scope_chars)
    trimmed = auth_db.get_prompt_context(uid, scope_chars, max_chars=100)
    check('бюджет символов оставляет только свежее сообщение',
          [c['message'] for c in trimmed] == ['О' * 80], [len(c['message']) for c in trimmed])

    # Лимит по числу сообщений
    scope_limit = 'test:' + uuid.uuid4().hex[:10]
    for i in range(5):
        auth_db.save_message(uid, 'user', f'вопрос {i}', device_id=scope_limit)
    last2 = auth_db.get_prompt_context(uid, scope_limit, limit=2, max_chars=4000)
    check('limit=2 берёт два последних сообщения',
          [c['message'] for c in last2] == ['вопрос 3', 'вопрос 4'], last2)
    return scope


# === 4) HTTP-поток =================================================

class _FakeRAG:
    def __init__(self):
        self.settings = _Settings()
        self.calls = []

    def ask_model(self, question, user_prompt=None, history=None):
        self.calls.append({'question': question, 'history': history})
        return f'ОТВЕТ {len(self.calls)}'


def _restore_setting(key, old_value, existed):
    if existed:
        auth_db.set_settings({key: old_value})
    else:
        conn = auth_db.get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM app_settings WHERE key = %s', (key,))
        conn.commit()
        cur.close()
        conn.close()


def test_http_flow(uid):
    print('\n4) HTTP-поток: /ask, «Новый диалог», виджет, настройки')
    fake = _FakeRAG()
    web_app.get_user_rag = lambda user_id: fake
    web_app.rag_ready = True

    def prompt_of(call):
        """Что реально уйдёт модели: история после отбрасывания текущего вопроса."""
        return rag_core._prepare_chat_history(call['history'], call['question'])

    client = web_app.app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['username'] = 'testmem'

    dev = 'testweb:' + uuid.uuid4().hex[:10]
    cookie = {'Cookie': f'device_id={dev}'}
    web_app._rate_hits.clear()                       # лимит /ask не мешает тесту

    r = client.post('/ask', json={'question': 'Сколько стоит насос X?'}, headers=cookie)
    ans1 = r.get_json().get('answer')
    check('первый /ask: ответ 200', r.status_code == 200 and ans1, r.status_code)
    check('первый /ask: памяти ещё нет (в промпт идёт только текущий вопрос)',
          prompt_of(fake.calls[0]) == [], fake.calls[0])

    client.post('/ask', json={'question': 'а второй?'}, headers=cookie)
    hist = prompt_of(fake.calls[-1])
    check('второй /ask: в промпт идут вопрос и ответ прошлого хода',
          [h.get('message') for h in hist] == ['Сколько стоит насос X?', ans1], hist)
    check('второй /ask: роли user/assistant', [h['role'] for h in hist] == ['user', 'assistant'], hist)
    check('текущий вопрос не дублируется в истории',
          all(h.get('message') != 'а второй?' for h in hist), hist)

    r = client.post('/api/chat/new', headers=cookie)
    check('«Новый диалог» отвечает success', r.status_code == 200 and r.get_json().get('success'), r.get_data(as_text=True))
    client.post('/ask', json={'question': 'третий вопрос'}, headers=cookie)
    check('после «Нового диалога» память пуста (только текущий вопрос)',
          prompt_of(fake.calls[-1]) == [], fake.calls[-1])

    r = client.get('/history', headers=cookie)
    body = r.get_json()
    check('/history отдаёт границу диалога и метки времени',
          body.get('session_started_at', 0) > 0 and all('ts' in m for m in body['messages']),
          {'started': body.get('session_started_at'), 'n': len(body.get('messages', []))})

    # Виджет: своя область, история владельца не видна
    ok, widget = auth_db.create_widget(uid, 'test-widget')
    check('тестовый виджет создан', ok and widget, widget)
    visitor = 'visitor_' + uuid.uuid4().hex[:12]
    wbody = {'key': widget['key'], 'visitor_id': visitor, 'site': '', 'question': 'привет из виджета'}
    r = client.post('/api/widget/ask', json=wbody)
    wans1 = r.get_json().get('answer')
    check('виджет ответил', r.status_code == 200 and wans1, r.get_data(as_text=True)[:200])
    check('виджет НЕ видит историю владельца',
          prompt_of(fake.calls[-1]) == [] and
          all('Сколько стоит насос X?' not in (h.get('message') or '') for h in fake.calls[-1]['history']),
          fake.calls[-1])
    client.post('/api/widget/ask', json=dict(wbody, question='второй вопрос виджета'))
    wh = prompt_of(fake.calls[-1])
    check('виджет помнит свой диалог',
          [h.get('message') for h in wh] == ['привет из виджета', wans1], wh)

    r = client.post('/api/widget/new', json={'key': widget['key'], 'visitor_id': visitor, 'site': ''})
    check('«Новый диалог» в виджете отвечает success', r.status_code == 200 and r.get_json().get('success'),
          r.get_data(as_text=True))
    client.post('/api/widget/ask', json=dict(wbody, question='после нового диалога'))
    check('виджет после «Нового диалога» начинает с чистого листа',
          prompt_of(fake.calls[-1]) == [], fake.calls[-1])

    # Настройки: память можно выключить
    settings_now = auth_db.get_all_settings()
    try:
        auth_db.set_settings({'chat_memory_enabled': False, 'chat_memory_external': False})
        web_app._rate_hits.clear()
        r = client.post('/ask', json={'question': 'вопрос при выключенной памяти'}, headers=cookie)
        check('chat_memory_enabled=false: веб-чат без истории',
              fake.calls[-1]['history'] == [], fake.calls[-1]['history'])
        client.post('/api/widget/ask', json=dict(wbody, question='виджет при выключенной памяти'))
        check('chat_memory_external=false: виджет без истории',
              fake.calls[-1]['history'] == [], fake.calls[-1]['history'])
    finally:
        for key in ('chat_memory_enabled', 'chat_memory_external'):
            _restore_setting(key, settings_now.get(key), key in settings_now)
    auth_db.delete_widget(uid, widget['id'])



def test_chat_label_follows_settings(uid):
    """Метка [провайдер | модель] в чат-логе берётся из СВЕЖИХ настроек.

    Раньше значения читались ДО ask_model() (только он делал reload()), поэтому
    первая реплика после смены настроек помечалась прежней моделью: админ менял
    модель, а в логе и истории оставалась старая — при том что отвечала уже новая.
    """
    print('\n3b) Метка чат-лога следует за сменой настроек')
    fake = _FakeRAG()
    fake.settings = _Settings(llm_provider='DashScope', llm_model='qwen-turbo')
    web_app.get_user_rag = lambda user_id: fake
    web_app.rag_ready = True

    logged = []
    real_log = web_app.chat_logger.log_message
    web_app.chat_logger.log_message = lambda *a, **kw: logged.append(kw)
    try:
        client = web_app.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = uid
            sess['username'] = 'testlabel'
        cookie = {'Cookie': 'device_id=label_' + uuid.uuid4().hex[:8]}
        web_app._rate_hits.clear()

        r = client.post('/ask', json={'question': 'метка до смены'}, headers=cookie)
        check('метка ответа = текущие настройки',
              r.status_code == 200 and logged[-1].get('provider') == 'DashScope'
              and logged[-1].get('model') == 'qwen-turbo', logged[-1])

        # «Админ» поменял провайдера/модель в БД: снимок обновится только в reload()
        fake.settings.db.update({'llm_provider': 'Local (llama.cpp)',
                                 'llm_model': 'qwen3-4b-instruct-2507'})
        web_app._rate_hits.clear()
        client.post('/ask', json={'question': 'метка после смены'}, headers=cookie)
        check('метка обновилась сразу, не отставая на один запрос',
              logged[-1].get('provider') == 'Local (llama.cpp)'
              and logged[-1].get('model') == 'qwen3-4b-instruct-2507', logged[-1])
    finally:
        web_app.chat_logger.log_message = real_log


def main():
    auth_db.init_db()
    auth_db.init_chat_history()
    uid, name = _make_test_user()
    print(f'Тестовый пользователь: {name} (id={uid})')
    try:
        test_history_helpers()
        test_ask_model_messages()
        test_qa_corrections()
        test_llm_payload()
        test_db_layer(uid)
        test_chat_label_follows_settings(uid)
        test_http_flow(uid)
    finally:
        auth_db.delete_user(uid)
        print(f'\nТест-пользователь {name} удалён (вместе с его историей/виджетом)')

    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for name in FAILED:
        print(f'  ❌ {name}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
