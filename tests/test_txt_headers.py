# test_txt_headers.py — строки-заголовки «# ...» не попадают в индекс (rag_core._process_file)
"""Проверка фильтра заголовков в TXT-файлах базы знаний.

Зачем: формат БЗ разрешает разделители «# Раздел» (они помогают человеку навигировать
по файлу, и именно так размечены страницы в файлах, собранных обходом сайта), но в
индекс эмбеддингов/BM25 они попадать не должны — иначе поиск засоряется короткими
служебными строками. Фильтр живёт в txt-ветке `_process_file`.

Запуск:  venv/Scripts/python.exe tests/test_txt_headers.py   (Linux-сервер: venv/bin/python tests/test_txt_headers.py)

БД и LLM не нужны: `auth_db` подменяется заглушкой (rag_core при импорте читает оттуда
настройки), сам метод вызывается на объекте, созданном через `object.__new__` — ветка
TXT не использует состояние экземпляра, поэтому модель эмбеддингов не грузится.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Заглушка auth_db: rag_core при импорте читает настройки (get_all_settings) и создаёт
# клиента LLM. Ключ-пустышка нужен только чтобы конструирование клиента не падало.
# Объявляем ровно те имена, которые rag_core берёт из auth_db (проверено поиском по коду);
# __file__ нужен, чтобы inspect внутри torch не спотыкался о модуль без файла.
if 'auth_db' not in sys.modules:
    stub = types.ModuleType('auth_db')
    stub.__file__ = __file__
    stub.get_all_settings = lambda: {'llm_api_key': 'test-key-not-used'}
    stub.set_settings = lambda settings: True
    stub.search_price_items = lambda *args, **kwargs: []
    sys.modules['auth_db'] = stub

import rag_core


KB_SAMPLE = """# Термочехлы AQ — раздел каталога
# Сайт example.com — https://example.com/catalog
Термочехлы AQ изготовлены из многослойного материала на основе хлоропренового каучука.

Срок изготовления съёмных термочехлов — пять рабочих дней с момента оплаты заказа.
# Термочехлы AQ — раздел доставки
Доставка термочехлов по России занимает от двух до семи дней в зависимости от региона.
"""


class TxtHeaderFilterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rag_txt_headers_')
        self.path = os.path.join(self.tmp, 'site_example.com.txt')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(KB_SAMPLE)

    def _fragments(self):
        core = object.__new__(rag_core.RAGCore)      # txt-ветка не трогает состояние
        knowledge = {}
        core._process_file(self.path, knowledge)
        return knowledge[self.path]

    def test_header_lines_are_not_indexed(self):
        fragments = self._fragments()
        self.assertTrue(fragments, 'файл не должен оказаться пустым')
        for line in fragments:
            self.assertFalse(line.startswith('#'), f'заголовок попал в индекс: {line}')
        self.assertFalse(any('example.com' in line for line in fragments))

    def test_facts_are_indexed_and_empty_lines_skipped(self):
        fragments = self._fragments()
        self.assertEqual(len(fragments), 3)
        self.assertTrue(any('хлоропренового каучука' in line for line in fragments))
        self.assertTrue(any('пять рабочих дней' in line for line in fragments))
        self.assertTrue(any('от двух до семи дней' in line for line in fragments))
        self.assertNotIn('', fragments)

    def test_leading_spaces_before_hash_also_skipped(self):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('   # Заголовок с отступом\n')
            f.write('Факт после заголовка с отступом: термочехлы шьются по индивидуальным размерам.\n')
        fragments = self._fragments()
        self.assertFalse(any(line.lstrip().startswith('#') for line in fragments))
        self.assertTrue(any('по индивидуальным размерам' in line for line in fragments))


if __name__ == '__main__':
    unittest.main(verbosity=2)
