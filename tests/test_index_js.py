# test_index_js.py — синтаксис JavaScript во встроенных скриптах интерфейса
"""Проверка, что JS из templates/index.html разбирается без ошибок.

Зачем: интерфейс — один большой блок <script> внутри Jinja-шаблона (десятки тысяч
символов). Опечатка в нём не ломает Python и не видна в тестах маршрутов, но
интерфейс перестаёт работать целиком. Проверяем через `node --check`: если node
не установлен, тест пропускается (не падает) — проверка необязательная.

Запуск:  venv/Scripts/python.exe tests/test_index_js.py   (Linux: venv/bin/python tests/test_index_js.py)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT / 'templates' / 'index.html'


class IndexJsCase(unittest.TestCase):
    def test_scripts_are_valid_javascript(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node не установлен — проверка JS пропущена')

        html = TEMPLATE.read_text(encoding='utf-8')
        blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
        self.assertTrue(blocks, 'в шаблоне не найдено встроенных скриптов')

        # Jinja-вставки внутри JS заменяем заглушками: они подставляются сервером
        # и синтаксис JS не описывают ({# комментарий #} узлом не является).
        js = '\n'.join(blocks)
        js = re.sub(r'\{\{.*?\}\}', "'JINJA'", js, flags=re.S)
        js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'index.js'
            target.write_text(js, encoding='utf-8')
            result = subprocess.run([node, '--check', str(target)],
                                    capture_output=True, text=True)

        self.assertEqual(result.returncode, 0,
                         f'JS в index.html не разбирается:\n{result.stderr[:2000]}')

    def test_dialog_wiring_is_present(self):
        """Галочка раздела и предпросмотр должны быть связаны с маршрутами."""
        html = TEMPLATE.read_text(encoding='utf-8')
        for expected in ('id="siteSection"', 'id="sitePreview"', "id='siteSection'",
                         '/api/site/preview?url=', 'section: document.getElementById'):
            if expected == "id='siteSection'":
                continue
            self.assertIn(expected, html, f'в интерфейсе нет: {expected}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
