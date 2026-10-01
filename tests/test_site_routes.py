# test_site_routes.py — маршруты индексации сайта в веб-приложении (web_app.py)
"""Сквозная проверка фичи «🌐 Индексировать сайт» без настоящей БД и без LLM.

Запуск:  venv/Scripts/python.exe tests/test_site_routes.py   (Linux-сервер: venv/bin/python tests/test_site_routes.py)

Подробности: обход идёт по локальному тестовому сайту (http.server) с флагом
SITE_CRAWLER_ALLOW_LOCAL=1, запись документа и переиндексация подменены заглушками
(add_document / get_user_documents / _reindex_user_async) — реальные базы и файлы
пользователей не затрагиваются. Рабочий каталог — временный.
"""

import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(__file__).resolve().parent.parent  # корень проекта (сами тесты лежат в tests/)
sys.path.insert(0, str(PROJECT))

PAGES = {
    "index.html": '<html><head><title>Демо-сайт AQ</title><meta name="description" '
                  'content="Демонстрационный сайт для проверки индексации: термочехлы и изоляция.">'
                  '</head><body><div class="menu"><a href="/about.html">О нас</a></div>'
                  '<h1>Термочехлы для оборудования</h1>'
                  '<p>Съёмные термочехлы изготавливаются из многослойного материала на основе '
                  'хлоропренового каучука и служат не менее десяти лет.</p></body></html>',
    "about.html": '<html><head><title>О нас</title></head><body>'
                  '<p>Производство термочехлов работает с 2012 года, выпуск более двух тысяч изделий в год.</p>'
                  '</body></html>',
    # раздел каталога — для проверки постраничной индексации (галочка «Только этот раздел»)
    "catalog/index.html": '<html><head><title>Каталог термочехлов</title></head><body>'
                          '<p>В каталоге собраны термочехлы для трубопроводов, насосов и запорной арматуры.</p>'
                          '<a href="/catalog/aeratsiya/index.html">Аэрация</a>'
                          '<a href="/catalog-sale/index.html">Распродажа</a></body></html>',
    "catalog/aeratsiya/index.html": '<html><head><title>Аэрация для воды</title></head><body>'
                                    '<p>Аэрационные колонны удаляют железо и сероводород, подбираются по расходу воды.</p>'
                                    '</body></html>',
    "catalog-sale/index.html": '<html><head><title>Распродажа</title></head><body>'
                               '<p>Ликвидация складских остатков: скидки на термочехлы до конца месяца.</p>'
                               '</body></html>',
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SiteRouteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_site_routes_")
        cls.src = os.path.join(cls.tmp, "src")
        os.makedirs(cls.src, exist_ok=True)
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)                                    # Database/ и site_cache/ — в temp
        for name, content in PAGES.items():
            path = os.path.join(cls.src, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)   # у каталога есть подпапки
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        handler = lambda *a, **kw: _Handler(*a, directory=cls.src, **kw)
        cls.server = _Server(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        os.environ["SITE_CRAWLER_ALLOW_LOCAL"] = "1"
        # web_app импортирует rag_core, а тот на старте создаёт клиента LLM из настроек
        # в PostgreSQL. На машине без БД ключа нет и импорт падает — для проверки
        # маршрутов сайта подставляем минимальную заглушку rag_core (сами маршруты
        # его не используют: переиндексация ниже тоже подменена).
        if 'rag_core' not in sys.modules:
            stub = types.ModuleType('rag_core')
            stub.DEFAULT_BASE_PROMPT = 'тестовый промт'
            stub.QA_CORRECTION_FILE = 'newdatabase.csv'
            for name in ('get_rag_system', 'get_user_rag', 'drop_user_rag', 'RAGSettings',
                         'build_greeting', 'parse_price_list', 'detect_doc_group',
                         '_read_text_preview', 'parse_qa_pairs_file', '_iter_csv_rows'):
                setattr(stub, name, lambda *a, **kw: None)
            sys.modules['rag_core'] = stub
        import site_crawler as sc
        import web_app
        cls.sc, cls.web_app = sc, web_app

        # Заглушки хранилища и переиндексации: БД и модель эмбеддингов не нужны
        cls.docs, cls.reindexed = [], []

        def fake_get_user_documents(user_id):
            return list(cls.docs)

        def fake_add_document(user_id, filename, original_name, file_hash=None, doc_group=''):
            cls.docs = [d for d in cls.docs if d['filename'] != filename]
            cls.docs.append({'id': len(cls.docs) + 1, 'filename': filename,
                             'original_name': original_name, 'doc_group': doc_group})
            return True, cls.docs[-1]['id']

        def fake_delete_document(doc_id, user_id):
            cls.docs = [d for d in cls.docs if d['id'] != doc_id]
            return True

        web_app.get_user_documents = fake_get_user_documents
        web_app.add_document = fake_add_document
        web_app.delete_document = fake_delete_document
        web_app._reindex_user_async = lambda user_id: cls.reindexed.append(user_id)
        # Флаг «модель прогрета» в тесте выставляем сами: иначе фоновая переиндексация
        # после обхода не запускается (в бою его ставит прогрев при старте сервера)
        web_app.rag_ready = True

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.environ.pop("SITE_CRAWLER_ALLOW_LOCAL", None)
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.docs.clear()
        self.reindexed.clear()
        shutil.rmtree(os.path.join(self.sc.CACHE_ROOT, "user_990003"), ignore_errors=True)
        shutil.rmtree(os.path.join("Database", "user_990003"), ignore_errors=True)
        deadline = time.time() + 30
        while time.time() < deadline and self.sc._JOB["active"]:
            time.sleep(0.2)
        with self.sc._JOB_LOCK:
            self.sc._JOB.update(active=False, job=None, finished_at=0.0)
        self.client = self.web_app.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 990003
            sess['username'] = 'site-tester'

    def _wait_done(self, timeout=90):
        deadline = time.time() + timeout
        state = {}
        while time.time() < deadline:
            state = self.client.get('/api/site/status').get_json()
            if not state.get('active'):
                return state
            time.sleep(0.25)
        self.fail(f"обход не завершился за {timeout} с: {state}")

    def test_crawl_route_end_to_end(self):
        response = self.client.post('/api/site/crawl',
                                    json={'url': self.base + '/index.html', 'pages': 5,
                                          'respect_robots': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])

        state = self._wait_done()
        job = state['job']
        self.assertEqual(job['phase'], 'index')
        self.assertTrue(job['indexed'])
        self.assertGreater(job['lines'], 0)

        txt = os.path.join('Database', 'user_990003', 'site_127.0.0.1.txt')
        self.assertTrue(os.path.exists(txt), 'TXT-файл сайта не создан')
        with open(txt, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('хлоропренового каучука', text)
        self.assertIn('с 2012 года', text)

        self.assertEqual([d['filename'] for d in self.docs], ['site_127.0.0.1.txt'])
        self.assertEqual(self.docs[0]['doc_group'], '🌐 127.0.0.1')
        self.assertEqual(self.reindexed, [990003])

        # Кнопка «⟳ Обновить с сайта»: адрес и лимит берутся из манифеста обхода
        info = self.client.get('/api/site/info?filename=site_127.0.0.1.txt').get_json()
        self.assertEqual(info['url'], self.base + '/index.html')
        self.assertEqual(info['page_limit'], 5)

    def test_recrawl_replaces_document_without_duplicates(self):
        self.client.post('/api/site/crawl', json={'url': self.base + '/index.html', 'pages': 5})
        self._wait_done()
        self.client.post('/api/site/crawl', json={'url': self.base + '/index.html', 'pages': 5})
        state = self._wait_done()
        self.assertEqual(state['job']['phase'], 'index')
        self.assertEqual(len(self.docs), 1, 'повторный обход не должен создавать дубликат документа')
        self.assertEqual(self.reindexed, [990003, 990003])

    def test_url_validation_and_auth(self):
        # Внутренние адреса запрещены (проверка идёт с боевыми настройками)
        os.environ.pop('SITE_CRAWLER_ALLOW_LOCAL', None)
        try:
            for url in ('ftp://x.test/', 'http://169.254.169.254/latest/meta-data/',
                        'http://10.0.0.7/', 'file:///etc/passwd', 'http://[::1]/'):
                r = self.client.post('/api/site/crawl', json={'url': url})
                self.assertEqual(r.status_code, 400, url)
                self.assertTrue(r.get_json()['error'])
            r = self.client.post('/api/site/crawl', json={'url': 'http://192.168.1.1/'})
            self.assertIn('внутренние', r.get_json()['error'].lower())
        finally:
            os.environ['SITE_CRAWLER_ALLOW_LOCAL'] = '1'
        # Пустой адрес
        self.assertEqual(self.client.post('/api/site/crawl', json={'url': '  '}).status_code, 400)
        # Без авторизации
        anon = self.web_app.app.test_client()
        self.assertEqual(anon.post('/api/site/crawl', json={'url': 'https://example.com'}).status_code, 401)
        self.assertEqual(anon.get('/api/site/status').status_code, 401)
        self.assertEqual(anon.get('/api/site/info?filename=x.txt').status_code, 401)

    def test_second_crawl_rejected_while_running(self):
        first = self.client.post('/api/site/crawl', json={'url': self.base + '/index.html', 'pages': 5})
        self.assertEqual(first.status_code, 200)
        second = self.client.post('/api/site/crawl', json={'url': self.base + '/about.html', 'pages': 5})
        self.assertIn(second.status_code, (200, 400))
        self._wait_done()

    def test_cancel_route_rules(self):
        sc = self.sc
        real_status, real_cancel = sc.get_status, sc.cancel
        called = []
        sc.cancel = lambda: (called.append(True) or True)
        try:
            sc.get_status = lambda: {'active': True, 'job': {'user_id': 999}}
            self.assertEqual(self.client.post('/api/site/cancel').status_code, 409)
            sc.get_status = lambda: {'active': True, 'job': {'user_id': 990003}}
            self.assertEqual(self.client.post('/api/site/cancel').get_json()['ok'], True)
            self.assertEqual(called, [True])
            sc.get_status = lambda: {'active': False, 'job': {}}
            self.assertEqual(self.client.post('/api/site/cancel').status_code, 409)
        finally:
            sc.get_status, sc.cancel = real_status, real_cancel

    def test_section_crawl_route_makes_separate_document(self):
        """Галочка «Только этот раздел»: свой файл БЗ, своё имя документа, свой манифест."""
        response = self.client.post('/api/site/crawl',
                                    json={'url': self.base + '/catalog/', 'pages': 5,
                                          'section': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['job']['section'], '/catalog')

        state = self._wait_done()
        job = state['job']
        self.assertEqual(job['phase'], 'index')

        txt = os.path.join('Database', 'user_990003', 'site_127.0.0.1_catalog.txt')
        self.assertTrue(os.path.exists(txt), 'TXT раздела не создан')
        with open(txt, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('термочехлы для трубопроводов', text)
        self.assertIn('Аэрационные колонны', text)
        self.assertNotIn('с 2012 года', text, 'в файл раздела попало содержимое всего сайта')
        self.assertNotIn('Ликвидация складских остатков', text)

        self.assertEqual([d['filename'] for d in self.docs], ['site_127.0.0.1_catalog.txt'])
        self.assertEqual(self.docs[0]['doc_group'], '🌐 127.0.0.1 · /catalog')
        self.assertEqual(self.reindexed, [990003])

        # «⟳ Обновить с сайта» у файла раздела: адрес, лимит и режим берутся из манифеста
        info = self.client.get('/api/site/info?filename=site_127.0.0.1_catalog.txt').get_json()
        self.assertEqual(info['section'], '/catalog')
        self.assertEqual(info['url'], self.base + '/catalog')
        self.assertEqual(info['page_limit'], 5)

    def test_check_route_returns_report(self):
        """/api/site/check отдаёт отчёт проверки и не пускает без входа."""
        report = {'ok': True, 'domain': '127.0.0.1', 'supported': True, 'reason': '',
                  'sitemap_urls': 3, 'known_urls': 2, 'files_total': 1, 'files_affected': 1,
                  'new': 1, 'changed': 0, 'gone': 0, 'unknown': 0, 'checked_at': '2026-10-01T10:00:00',
                  'files': [{'section': '/catalog', 'filename': 'site_127.0.0.1_catalog.txt',
                             'new': 1, 'changed': 0, 'gone': 0, 'pages': 2}]}
        calls = []
        real = self.sc.check_site_changes

        def fake_check(user_id, url, respect_robots=True):
            calls.append((user_id, url, respect_robots))
            return dict(report)

        self.sc.check_site_changes = fake_check
        try:
            response = self.client.post('/api/site/check',
                                        json={'url': self.base + '/catalog/', 'respect_robots': False})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['new'], 1)
            self.assertEqual(calls, [(990003, self.base + '/catalog/', False)])
            # адрес без схемы дополняется https
            self.client.post('/api/site/check', json={'url': 'example.com'})
            self.assertEqual(calls[-1][1], 'https://example.com')
        finally:
            self.sc.check_site_changes = real

        self.assertEqual(self.client.post('/api/site/check', json={}).status_code, 400)

    def test_long_section_label_fits_group_column(self):
        """Группа документа влезает в VARCHAR(100): иначе add_document падает.

        У глубокого раздела путь длиннее колонки, вставка падала с «value too long»,
        и файл обхода не попадал в базу знаний.
        """
        section = ('/catalog/komplektuyushchie_dlya_sistem_vodoochistki/'
                   'setchatye_diskovye_meshochnye_filtry_2')
        label = self.web_app._site_group_label('127.0.0.1', section)
        self.assertLessEqual(len(label), self.web_app.SITE_GROUP_MAX_LEN)
        self.assertTrue(label.startswith('🌐 127.0.0.1 · …'), label)
        self.assertTrue(label.endswith(section[-10:]), label)
        self.assertIn('/', label, 'обрезали посреди имени раздела')
        # короткий раздел не трогаем
        self.assertEqual(self.web_app._site_group_label('127.0.0.1', '/catalog'),
                         '🌐 127.0.0.1 · /catalog')
        self.assertEqual(self.web_app._site_group_label('127.0.0.1', ''), '🌐 127.0.0.1')

    def test_cancelled_crawl_still_indexes_written_file(self):
        """Обход отменили, но файл записан — он обязан попасть в базу знаний.

        Иначе в индексе остаётся прежняя версия файла, а на диске уже другая:
        ассистент отвечает по устаревшим данным, и «файла эмбеддинга» для нового
        документа не появляется.
        """
        sc, real_crawl = self.sc, self.sc.crawl
        filename = 'site_127.0.0.1_catalog.txt'

        def fake_crawl(user_id, url, limit, respect_robots=True, on_progress=None,
                       cancel_event=None, section=False):
            folder = os.path.join('Database', f'user_{user_id}')
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, filename)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('# Каталог — ' + url + '\n'
                        'Термочехлы для трубопроводов поставляются со склада в Москве в течение суток.\n')
            return {'ok': True, 'domain': '127.0.0.1', 'filename': filename,
                    'txt_path': os.path.abspath(path), 'lines': 1, 'total_lines': 2,
                    'section': '/catalog', 'url': url, 'page_limit': limit,
                    'respect_robots': respect_robots,
                    'stats': {'cancelled': True, 'pages': 1, 'lines': 1}}

        sc.crawl = fake_crawl
        try:
            response = self.client.post('/api/site/crawl',
                                        json={'url': self.base + '/catalog/', 'pages': 5,
                                              'section': True})
            self.assertEqual(response.status_code, 200)
            state = self._wait_done()
        finally:
            sc.crawl = real_crawl

        job = state['job']
        self.assertEqual(job['phase'], 'cancelled')
        self.assertTrue(job.get('indexed'), 'отменённый обход не добавил файл в базу знаний')
        self.assertEqual([d['filename'] for d in self.docs], [filename])
        self.assertEqual(self.docs[0]['doc_group'], '🌐 127.0.0.1 · /catalog')
        self.assertEqual(self.reindexed, [990003])

    def test_documents_panel_indexes_new_files_from_disk(self):
        """Файл появился в папке пользователя — панель обязана запустить переиндексацию."""
        web_app = self.web_app
        folder = os.path.join('Database', 'user_990003')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'dropped.txt'), 'w', encoding='utf-8') as f:
            f.write('Термочехлы изготавливаются из многослойного материала и служат не менее десяти лет.\n')

        real_price = web_app.get_price_files
        real_backfill = web_app._backfill_doc_groups_async
        web_app.get_price_files = lambda user_id: {}
        web_app._backfill_doc_groups_async = lambda user_id, docs: None
        try:
            response = self.client.get('/api/documents')
        finally:
            web_app.get_price_files = real_price
            web_app._backfill_doc_groups_async = real_backfill

        self.assertEqual(response.status_code, 200)
        self.assertIn('dropped.txt', [d['filename'] for d in self.docs],
                      'файл с диска не зарегистрирован в базе знаний')
        self.assertEqual(self.reindexed, [990003],
                         'новый файл на диске не запустил переиндексацию')

    def test_preview_route_reports_split_plan(self):
        """Предпросмотр показывает, что большой раздел будет разбит на подразделы."""
        domain = '127.0.0.1'
        folder = os.path.join('site_cache', f'user_{990003}', domain)
        os.makedirs(folder, exist_ok=True)
        # Манифесты прошлых обходов в этом же временном каталоге дали бы другую плотность
        # строк на страницу — убираем их, чтобы бюджет файла считался по значению
        # по умолчанию (60 строк на страницу → около 166 страниц в файл)
        for name in os.listdir(folder):
            if name.startswith('manifest') and name.endswith('.json'):
                os.remove(os.path.join(folder, name))
        urls = [self.base + '/catalog/'] + [self.base + f'/catalog/s{i % 5}/{i}.html'
                                           for i in range(300)]
        with open(os.path.join(folder, 'sitemap_urls.json'), 'w', encoding='utf-8') as f:
            json.dump({'urls': urls}, f)

        response = self.client.get('/api/site/preview?url=' + self.base +
                                   '/catalog/&section=1&plan=1')
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        plan = data.get('split') or {}
        self.assertTrue(plan.get('needed'), 'план дробления не посчитан')
        self.assertEqual(plan.get('reason'), 'split')
        self.assertGreater(len(plan.get('parts') or []), 1)
        warning = ' '.join(data.get('warnings') or [])
        self.assertIn('разбит', warning)
        self.assertIn('подраздел', warning)

    def test_crawl_route_passes_split_flag(self):
        """Режим раздела запускает обход с дроблением и обработчиком частей."""
        captured = {}
        real_start = self.sc.start_job

        def fake_start_job(user_id, url, pages, respect_robots, **kwargs):
            captured.update(kwargs)
            return {'user_id': user_id, 'url': url, 'phase': 'crawl'}

        self.sc.start_job = fake_start_job
        try:
            response = self.client.post('/api/site/crawl',
                                        json={'url': self.base + '/catalog/', 'pages': 5,
                                              'section': True})
        finally:
            self.sc.start_job = real_start

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured.get('split'), 'дробление не включено')
        self.assertIsNotNone(captured.get('on_finish_parts'), 'нет обработчика частей')
        self.assertIsNotNone(captured.get('on_finish'))

    def test_preview_route_shows_filename_and_overlaps(self):
        preview = self.client.get('/api/site/preview?url=' + self.base + '/catalog/&section=1').get_json()
        self.assertTrue(preview['ok'])
        self.assertEqual(preview['filename'], 'site_127.0.0.1_catalog.txt')
        self.assertEqual(preview['section'], '/catalog')
        self.assertEqual(preview['mode'], 'section')
        self.assertEqual(preview['warnings'], [])

        # без галочки тот же адрес — это обход всего сайта
        whole = self.client.get('/api/site/preview?url=' + self.base + '/catalog/').get_json()
        self.assertEqual(whole['filename'], 'site_127.0.0.1.txt')
        self.assertEqual(whole['mode'], 'site')

        # галочка на главной странице смысла не имеет — предупреждаем
        home = self.client.get('/api/site/preview?url=' + self.base + '/&section=1').get_json()
        self.assertEqual(home['mode'], 'site')
        self.assertTrue(any('главная' in w for w in home['warnings']), home['warnings'])

        # сайт уже проиндексирован целиком → предупреждаем о пересечении
        self.client.post('/api/site/crawl', json={'url': self.base + '/index.html', 'pages': 5})
        self._wait_done()
        after = self.client.get('/api/site/preview?url=' + self.base + '/catalog/&section=1').get_json()
        self.assertTrue(any('site_127.0.0.1.txt' in w for w in after['warnings']), after['warnings'])
        site_preview = self.client.get('/api/site/preview?url=' + self.base + '/index.html').get_json()
        self.assertEqual(site_preview['warnings'], [])

    def test_preview_route_validation_and_auth(self):
        self.assertEqual(self.client.get('/api/site/preview?url=').status_code, 400)
        os.environ.pop('SITE_CRAWLER_ALLOW_LOCAL', None)
        try:
            blocked = self.client.get('/api/site/preview?url=http://10.0.0.7/catalog/')
            self.assertEqual(blocked.status_code, 400)
            self.assertTrue(blocked.get_json()['error'])
        finally:
            os.environ['SITE_CRAWLER_ALLOW_LOCAL'] = '1'
        anon = self.web_app.app.test_client()
        self.assertEqual(anon.get('/api/site/preview?url=https://example.com/').status_code, 401)

    def test_pages_limit_setting_is_respected_and_capped(self):
        """Лимит страниц: из app_settings, но не выше потолка краулера."""
        real_settings = self.web_app.get_all_settings
        try:
            self.web_app.get_all_settings = lambda: {}
            self.assertEqual(self.web_app._site_pages_limit(), self.sc.DEFAULT_PAGE_LIMIT)
            self.assertEqual(self.sc.DEFAULT_PAGE_LIMIT, 300)

            self.web_app.get_all_settings = lambda: {'site_pages_limit': 777}
            self.assertEqual(self.web_app._site_pages_limit(), 777)

            self.web_app.get_all_settings = lambda: {'site_pages_limit': 99999}
            self.assertEqual(self.web_app._site_pages_limit(), self.sc.MAX_PAGE_LIMIT)
            self.assertEqual(self.sc.MAX_PAGE_LIMIT, 1000)

            self.web_app.get_all_settings = lambda: {'site_pages_limit': 'мусор'}
            self.assertEqual(self.web_app._site_pages_limit(), self.sc.DEFAULT_PAGE_LIMIT)
        finally:
            self.web_app.get_all_settings = real_settings

    def test_status_hides_other_users_job(self):
        sc = self.sc
        real_status = sc.status_json
        sc.status_json = lambda: {'active': True, 'job': {'user_id': 424242, 'url': 'https://x.test/'}}
        try:
            data = self.client.get('/api/site/status').get_json()
            self.assertTrue(data['active'])
            self.assertTrue(data.get('other_user'))
            self.assertIsNone(data['job'])
        finally:
            sc.status_json = real_status


if __name__ == "__main__":
    unittest.main(verbosity=2)
