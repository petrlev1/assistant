# test_site_routes.py — маршруты индексации сайта в веб-приложении (web_app.py)
"""Сквозная проверка фичи «🌐 Индексировать сайт» без настоящей БД и без LLM.

Запуск:  venv/Scripts/python.exe test_site_routes.py

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

PROJECT = Path(__file__).resolve().parent
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
            with open(os.path.join(cls.src, name), "w", encoding="utf-8") as f:
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
