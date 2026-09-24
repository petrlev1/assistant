# test_admin_models.py — выбор моделей LLM/OCR в админ-панели (/admin)
"""Проверка блока «Модели» в админке и маршрута POST /admin/api/settings.

Запуск:  venv/Scripts/python.exe tests/test_admin_models.py   (Linux-сервер: venv/bin/python tests/test_admin_models.py)

БД и rag_core подменены заглушками: настоящие настройки, ключи и базы знаний не
затрагиваются. Рабочий каталог — временный (embeddings_cache OCR-теста уходит туда).
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT = Path(__file__).resolve().parent.parent

# Заглушка rag_core: настоящий тянет torch (~10 c) и лезет в БД за настройками
if 'rag_core' not in sys.modules:
    stub = types.ModuleType('rag_core')
    stub.DEFAULT_BASE_PROMPT = 'тестовый промт'
    stub.QA_CORRECTION_FILE = 'newdatabase.csv'
    for name in ('get_rag_system', 'get_user_rag', 'drop_user_rag', 'RAGSettings',
                 'build_greeting', 'parse_price_list', 'detect_doc_group',
                 '_read_text_preview', 'parse_qa_pairs_file', '_iter_csv_rows'):
        setattr(stub, name, lambda *a, **kw: None)
    sys.modules['rag_core'] = stub

import web_app  # noqa: E402

# «Боевые» настройки в тесте: значение ключа содержит метку LEAK — её быть на странице не должно
SETTINGS = {
    'llm_provider': 'DashScope',
    'llm_model': 'qwen-turbo',
    'llm_base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'disable_llm_models': False,
    'ocr_enabled': True,
    'ocr_model': 'qwen-vl-ocr',
    'ocr_base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'ocr_dpi': 150,
    'llm_api_key': 'sk-LEAK-dashscope-key',
    'llm_provider_api_key': 'sk-LEAK-deepseek-key',
    'llm_openrouter_api_key': '',
    'search_top_k': 30,
}


class AdminModelsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='rag_admin_models_')
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.saved = []
        web_app.get_all_settings = lambda: dict(SETTINGS)

        def fake_set_settings(mapping):
            cls.saved.append(dict(mapping))
            return True

        web_app.set_settings = fake_set_settings

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.saved.clear()
        self.client = web_app.app.test_client()

    def _as_admin(self):
        with self.client.session_transaction() as sess:
            sess['admin'] = True

    def _models_json(self):
        """Данные блока «Модели» — из JSON-блока страницы (JS рисует по ним поля)."""
        html = self.client.get('/admin').get_data(as_text=True)
        marker = '<script id="models-data" type="application/json">'
        start = html.index(marker) + len(marker)
        return json.loads(html[start:html.index('</script>', start)])

    # --- страница ---
    def test_01_login_mode_without_admin_session(self):
        html = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('Служебный вход', html)
        self.assertNotIn('id="llm_provider"', html)

    def test_02_dashboard_shows_models_block(self):
        self._as_admin()
        html = self.client.get('/admin').get_data(as_text=True)
        for marker in ('Модели', 'id="llm_provider"', 'id="llm_model"', 'id="ocr_model"',
                       'id="ocr_dpi"', 'id="llm_api_key"', 'id="llm_provider_api_key"',
                       'id="llm_openrouter_api_key"', 'id="search_top_k"',
                       'intfloat/multilingual-e5-large',
                       'id="models-data"', 'id="save-models"'):
            self.assertIn(marker, html, f'нет элемента: {marker}')

    def test_03_current_values_and_key_states(self):
        self._as_admin()
        data = self._models_json()
        self.assertEqual(data['values']['llm_model'], 'qwen-turbo')
        self.assertEqual(data['values']['ocr_model'], 'qwen-vl-ocr')
        self.assertEqual(data['values']['ocr_dpi'], 150)
        self.assertTrue(data['flags']['ocr_enabled'])
        self.assertEqual(data['key_states']['llm_api_key'], 'задан')
        self.assertEqual(data['key_states']['llm_provider_api_key'], 'задан')
        self.assertEqual(data['key_states']['llm_openrouter_api_key'], 'не задан')
        self.assertEqual(data['embedding_model'], 'intfloat/multilingual-e5-large')
        self.assertEqual(data['values']['search_top_k'], 30)
        self.assertEqual(sorted(data['providers']), ['DashScope', 'DeepSeek', 'OpenRouter'])
        self.assertIn('qwen-vl-max', data['ocr_models'])

    def test_04_secret_values_never_reach_the_page(self):
        self._as_admin()
        html = self.client.get('/admin').get_data(as_text=True)
        self.assertNotIn('LEAK', html)

    # --- сохранение ---
    def test_04b_tabs_default_to_users(self):
        """Две вкладки: «Пользователи» открыта по умолчанию, «Настройки» скрыта до клика."""
        self._as_admin()
        html = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('data-tab="users"', html)
        self.assertIn('data-tab="settings"', html)
        self.assertIn('<div class="tabpane" id="pane-settings">', html)          # без active — скрыта
        self.assertIn('<div class="tabpane active" id="pane-users">', html)     # открыта
        # блок «Модели» живёт в панели настроек, таблица пользователей — в панели пользователей
        self.assertLess(html.index('id="pane-settings"'), html.index('id="llm_provider"'))
        self.assertLess(html.index('id="llm_provider"'), html.index('id="pane-users"'))
        self.assertIn('showTab', html)

    def test_05_save_requires_admin(self):
        r = self.client.post('/admin/api/settings', json={'llm_provider': 'DeepSeek'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.saved, [])

    def test_06_unknown_provider_rejected(self):
        self._as_admin()
        body = {'llm_provider': 'НеТакой', 'llm_model': 'm', 'llm_base_url': 'https://x/v1',
                'ocr_model': 'qwen-vl-ocr', 'ocr_base_url': 'https://x/v1', 'ocr_dpi': 150}
        r = self.client.post('/admin/api/settings', json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn('провайдер', r.get_json()['error'])
        self.assertEqual(self.saved, [])

    def test_07_empty_model_and_bad_dpi_rejected(self):
        self._as_admin()
        base = {'llm_provider': 'DeepSeek', 'llm_model': 'deepseek-chat',
                'llm_base_url': 'https://api.deepseek.com/v1', 'ocr_model': 'qwen-vl-ocr',
                'ocr_base_url': 'https://x/v1', 'ocr_dpi': 150}
        r = self.client.post('/admin/api/settings', json={**base, 'llm_model': '  '})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Модель LLM'.lower(), r.get_json()['error'].lower())

        r = self.client.post('/admin/api/settings', json={**base, 'ocr_dpi': 5000})
        self.assertEqual(r.status_code, 400)
        self.assertIn('dpi', r.get_json()['error'].lower())

        r = self.client.post('/admin/api/settings', json={**base, 'llm_base_url': 'api.deepseek.com'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.saved, [])

    def test_08_save_without_keys_does_not_touch_keys(self):
        self._as_admin()
        body = {'llm_provider': 'DeepSeek', 'llm_model': 'deepseek-chat',
                'llm_base_url': 'https://api.deepseek.com/v1', 'ocr_model': 'qwen-vl-ocr',
                'ocr_base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
                'ocr_dpi': 200, 'ocr_enabled': True, 'disable_llm_models': False,
                'llm_api_key': '', 'llm_provider_api_key': '', 'llm_openrouter_api_key': ''}
        r = self.client.post('/admin/api/settings', json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(len(self.saved), 1)
        saved = self.saved[0]
        self.assertEqual(saved['llm_provider'], 'DeepSeek')
        self.assertEqual(saved['llm_model'], 'deepseek-chat')
        self.assertEqual(saved['ocr_dpi'], 200)
        for key in ('llm_api_key', 'llm_provider_api_key', 'llm_openrouter_api_key'):
            self.assertNotIn(key, saved, f'пустое поле не должно перезаписывать {key}')

    def test_09_save_with_key_writes_it_but_hides_value(self):
        self._as_admin()
        body = {'llm_provider': 'OpenRouter', 'llm_model': 'openai/gpt-4o-mini',
                'llm_base_url': 'https://openrouter.ai/api/v1', 'ocr_model': 'qwen-vl-plus',
                'ocr_base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
                'ocr_dpi': 150, 'llm_api_key': '', 'llm_provider_api_key': '',
                'llm_openrouter_api_key': 'sk-or-v1-test-key-123'}
        r = self.client.post('/admin/api/settings', json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved[0]['llm_openrouter_api_key'], 'sk-or-v1-test-key-123')
        self.assertNotIn('sk-or-v1-test-key-123', r.get_data(as_text=True))
        self.assertIn('ключи', r.get_json()['message'])

    def test_10_short_key_looks_like_typo(self):
        self._as_admin()
        body = {'llm_provider': 'DeepSeek', 'llm_model': 'deepseek-chat',
                'llm_base_url': 'https://api.deepseek.com/v1', 'ocr_model': 'qwen-vl-ocr',
                'ocr_base_url': 'https://x/v1', 'ocr_dpi': 150, 'llm_provider_api_key': 'sk-1'}
        r = self.client.post('/admin/api/settings', json=body)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.saved, [])

    def test_11_csrf_from_foreign_origin_rejected(self):
        self._as_admin()
        r = self.client.post('/admin/api/settings', json={'llm_provider': 'DeepSeek'},
                             headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)

    def test_11b_search_top_k_saved_as_number(self):
        """top-K поиска — одно общее значение для всех моделей, сохраняется числом."""
        self._as_admin()
        body = {'llm_provider': 'DeepSeek', 'llm_model': 'deepseek-chat',
                'llm_base_url': 'https://api.deepseek.com/v1', 'ocr_model': 'qwen-vl-ocr',
                'ocr_base_url': 'https://x/v1', 'ocr_dpi': 150, 'search_top_k': 12}
        r = self.client.post('/admin/api/settings', json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved[0]['search_top_k'], 12)
        self.assertIsInstance(self.saved[0]['search_top_k'], int)

    def test_11c_search_top_k_out_of_range_rejected(self):
        """0, 51 и мусор — ошибка; без ключа в запросе значение не трогаем."""
        self._as_admin()
        base = {'llm_provider': 'DeepSeek', 'llm_model': 'deepseek-chat',
                'llm_base_url': 'https://api.deepseek.com/v1', 'ocr_model': 'qwen-vl-ocr',
                'ocr_base_url': 'https://x/v1', 'ocr_dpi': 150}
        for bad in (0, 51, '', 'abc'):
            r = self.client.post('/admin/api/settings', json={**base, 'search_top_k': bad})
            self.assertEqual(r.status_code, 400, f'значение {bad!r} должно быть отклонено')
            self.assertIn('top-K', r.get_json()['error'])
        self.assertEqual(self.saved, [])

        r = self.client.post('/admin/api/settings', json=base)   # ключа нет — не трогаем
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn('search_top_k', self.saved[0])


class OcrCacheByModelCase(unittest.TestCase):
    """Кэш OCR-текста разбит по модели: смена ocr_model перечитывает страницы заново.

    Здесь импортируется НАСТОЯЩИЙ rag_core (torch + sentence-transformers), поэтому
    этот класс идёт последним и заметно медленнее остальных проверок файла.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='rag_ocr_cache_')
        cls.cwd = os.getcwd()
        # db_config.json читается из ТЕКУЩЕГО каталога: копируем его в temp, иначе
        # rag_core при импорте не получит настройки и упадёт на пустом ключе OpenAI.
        src_cfg = os.path.join(PROJECT, 'db_config.json')
        if os.path.isfile(src_cfg):
            shutil.copy(src_cfg, os.path.join(cls.tmp, 'db_config.json'))
        os.chdir(cls.tmp)
        cls.pdf = os.path.join(cls.tmp, 'scan.pdf')
        with open(cls.pdf, 'wb') as f:
            f.write(b'%PDF-1.4 test')

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _core(self, model):
        sys.modules.pop('rag_core', None)          # берём настоящий rag_core
        # На старте rag_core создаёт клиента OpenAI из настроек; на машине без ключа
        # в app_settings импорт падает «Missing credentials» — подставляем заглушку.
        os.environ.setdefault('OPENAI_API_KEY', 'test-key')
        import importlib
        real = importlib.import_module('rag_core')
        core = real.RAGCore.__new__(real.RAGCore)

        class _S:
            def get(self, key, default=None):
                return model if key == 'ocr_model' else default

        core.settings = _S()
        core.current_user_id = None
        return real, core

    def test_path_contains_model_and_differs_per_model(self):
        real, core = self._core('qwen-vl-ocr')
        try:
            p1 = core._get_ocr_cache_path(self.pdf, 0)
            self.assertIn('qwen-vl-ocr', str(p1).replace('\\', '/'))
            _, core2 = self._core('qwen-vl-plus')
            p2 = core2._get_ocr_cache_path(self.pdf, 0)
            self.assertIn('qwen-vl-plus', str(p2).replace('\\', '/'))
            self.assertNotEqual(str(p1), str(p2))
            self.assertEqual(real.RAGCore._ocr_model_slug('openai/gpt-4o'), 'openai_gpt-4o')
            self.assertEqual(real.RAGCore._ocr_model_slug(''), 'default')
        finally:
            sys.modules.pop('rag_core', None)   # вернуть состояние до теста (в web_app — заглушка)


if __name__ == '__main__':
    unittest.main(verbosity=2)
