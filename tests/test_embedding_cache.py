# test_embedding_cache.py — построчный кэш векторов (rag_core: .lines.pkl)
"""Проверка инкрементального пересчёта эмбеддингов.

Зачем: эмбеддинги файла БЗ хранятся одним pkl на файл с хэшем содержимого, поэтому
любое изменение (например, после повторного обхода раздела сайта) заставляло
пересчитывать эмбеддинги ВСЕГО файла. Построчный кэш (`<имя>.lines.pkl`) хранит
вектор на каждую строку по md5 её текста — пересчитываются только новые строки.

Здесь модель подменяется заглушкой, которая считает, сколько строк реально ушло
в `encode` — именно это и есть предмет проверки.

Запуск:  venv/Scripts/python.exe tests/test_embedding_cache.py   (Linux: venv/bin/python tests/test_embedding_cache.py)
"""

import hashlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Заглушка auth_db: rag_core при импорте читает настройки из БД.
if 'auth_db' not in sys.modules:
    stub = types.ModuleType('auth_db')
    stub.__file__ = __file__
    stub.get_all_settings = lambda: {'llm_api_key': 'test-key-not-used'}
    stub.set_settings = lambda settings: True
    stub.search_price_items = lambda *args, **kwargs: []
    sys.modules['auth_db'] = stub

import torch  # noqa: E402

import rag_core  # noqa: E402

USER_ID = 5
DOC = 'site_example.com_catalog.txt'


def _vector(text):
    digest = hashlib.md5(str(text).encode('utf-8')).digest()
    return [byte / 255.0 for byte in digest[:4]]


class StubModel:
    """Заглушка модели: считает, сколько строк ей реально отдали на эмбеддинг."""

    def __init__(self):
        self.device = torch.device('cpu')
        self.calls = []

    def encode(self, texts, convert_to_tensor=True):
        self.calls.append(list(texts))
        return torch.tensor([_vector(text) for text in texts], dtype=torch.float32)

    @property
    def embedded(self):
        return sum(len(call) for call in self.calls)


def make_core(model):
    """Объект RAGCore без загрузки модели: нужны только кэш-методы."""
    core = object.__new__(rag_core.RAGCore)
    core.current_user_id = USER_ID
    core.model = model
    core._state_lock = None
    return core


class EmbeddingCacheCase(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix='rag_lines_')
        os.chdir(self.tmp)
        os.makedirs(os.path.join('Database', f'user_{USER_ID}'), exist_ok=True)
        self.model = StubModel()
        self.core = make_core(self.model)
        self.doc_path = os.path.join('Database', f'user_{USER_ID}', DOC)

    def tearDown(self):
        os.chdir(self._cwd)

    def cache_dir(self):
        return Path('embeddings_cache') / f'user_{USER_ID}'

    def lines_path(self):
        return self.cache_dir() / f'{Path(DOC).stem}.lines.pkl'

    def run_index(self, lines):
        """Индексирует файл так же, как это делает перезагрузка БЗ."""
        Path(self.doc_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return self.core._load_or_create_embeddings(self.doc_path, lines)

    def test_first_index_embeds_all_lines_and_creates_cache(self):
        embeddings = self.run_index(['строка один', 'строка два', 'строка три'])
        self.assertEqual(self.model.embedded, 3)
        self.assertEqual(tuple(embeddings.shape), (3, 4))
        self.assertTrue(self.lines_path().exists(), 'построчный кэш не создан')

    def test_unchanged_file_does_not_embed_anything(self):
        first = self.run_index(['а', 'б', 'в'])
        before = self.model.embedded
        second = self.run_index(['а', 'б', 'в'])
        self.assertEqual(self.model.embedded, before, 'эмбеддинги пересчитаны без изменений')
        self.assertTrue(torch.equal(first, second))

    def test_changed_line_embeds_only_that_line(self):
        self.run_index(['а', 'б', 'в'])
        before = self.model.embedded
        embeddings = self.run_index(['а', 'б', 'изменённая'])
        self.assertEqual(self.model.embedded - before, 1, 'пересчитано не только изменённая строка')
        self.assertTrue(torch.equal(embeddings[0], torch.tensor(_vector('а'))))
        self.assertTrue(torch.equal(embeddings[2], torch.tensor(_vector('изменённая'))))

    def test_added_line_embeds_only_new_one(self):
        self.run_index(['а', 'б'])
        before = self.model.embedded
        embeddings = self.run_index(['а', 'б', 'новая'])
        self.assertEqual(self.model.embedded - before, 1)
        self.assertEqual(tuple(embeddings.shape), (3, 4))

    def test_duplicate_lines_are_embedded_once(self):
        embeddings = self.run_index(['повтор', 'другая', 'повтор'])
        self.assertEqual(self.model.embedded, 2, 'строка-дубликат посчитана дважды')
        self.assertTrue(torch.equal(embeddings[0], embeddings[2]))

    def test_cache_pruned_to_current_lines(self):
        self.run_index(['а', 'б', 'в', 'г', 'д'])
        self.run_index(['а', 'б'])
        keys, vectors = self.core._load_line_cache(self.doc_path)
        self.assertEqual(len(keys), 2)
        self.assertEqual(tuple(vectors.shape), (2, 4))

    def test_cleanup_keeps_lines_cache_of_existing_document(self):
        self.run_index(['а', 'б'])
        other = os.path.join('Database', f'user_{USER_ID}', 'other.txt')
        Path(other).write_text('в\n', encoding='utf-8')
        self.core._load_or_create_embeddings(other, ['в'])
        other_lines = self.cache_dir() / 'other.lines.pkl'
        self.assertTrue(other_lines.exists())

        self.core._cleanup_orphaned_cache({Path(DOC).stem})

        self.assertTrue(self.lines_path().exists(), 'построчный кэш живого файла удалён')
        self.assertFalse(other_lines.exists(), 'кэш удалённого файла остался')

    def test_migration_does_not_touch_other_document_cache(self):
        """Старый формат — <имя>_<md5>.pkl; кэш соседнего документа затронут быть не должен."""
        self.cache_dir().mkdir(parents=True, exist_ok=True)
        stem = Path(DOC).stem                       # site_example.com_catalog
        site_stem = 'site_example.com'              # файл всего сайта
        old_style = self.cache_dir() / f'{site_stem}_{hashlib.md5(b"x").hexdigest()}.pkl'
        with open(old_style, 'wb') as f:
            import pickle
            pickle.dump({'hash': 'x', 'embeddings': torch.zeros(1, 4)}, f)
        neighbour = self.cache_dir() / f'{stem}.pkl'          # кэш раздела (наш файл)
        neighbour_lines = self.cache_dir() / f'{stem}.lines.pkl'
        for path in (neighbour, neighbour_lines):
            with open(path, 'wb') as f:
                import pickle
                pickle.dump({'keys': [], 'vectors': torch.zeros(0, 4)}, f)

        migrated = self.core._migrate_old_cache_files(
            os.path.join('Database', f'user_{USER_ID}', f'{site_stem}.txt'), ['x'])

        self.assertIsNotNone(migrated, 'старый кэш не мигрирован')
        self.assertFalse(old_style.exists(), 'старый кэш не удалён после миграции')
        self.assertTrue(neighbour.exists(), 'удалён кэш другого документа')
        self.assertTrue(neighbour_lines.exists(), 'удалён построчный кэш другого документа')


if __name__ == '__main__':
    unittest.main(verbosity=2)
