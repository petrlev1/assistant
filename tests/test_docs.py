# test_docs.py — проверки пользовательской документации раздела /docs
"""Проверяет, что документация в docs/*.md согласована с меню раздела /docs:

* у каждой страницы из DOCS_PAGES (web_app.py) есть файл docs/<slug>.md;
* все ссылки вида /docs/<slug> из markdown ведут на существующие страницы;
* каждая страница рендерится мини-рендерером без остатков markdown-разметки;
* нет «осиротевших» файлов, которых нет в меню (иначе о них нельзя узнать).

Запуск:  venv/Scripts/python.exe tests/test_docs.py   (Linux-сервер: venv/bin/python tests/test_docs.py)
"""

import os
import re
import sys
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(__file__).resolve().parent.parent  # корень проекта (сами тесты лежат в tests/)
sys.path.insert(0, str(PROJECT))

import docs_renderer

DOCS_DIR = PROJECT / 'docs'
MENU_BLOCK = re.compile(r'DOCS_PAGES = \[(.*?)\]', re.S)
MENU_ITEM = re.compile(r"\('([a-z0-9_-]+)',\s*'[^']+'\)")


def menu_slugs():
    """Slug'и страниц из меню /docs — в порядке боковой панели."""
    source = (PROJECT / 'web_app.py').read_text(encoding='utf-8')
    match = MENU_BLOCK.search(source)
    assert match, 'не найден блок DOCS_PAGES в web_app.py'
    return MENU_ITEM.findall(match.group(1))


def doc_files():
    return {p.stem for p in DOCS_DIR.glob('*.md')}


class DocsCase(unittest.TestCase):
    def test_every_menu_page_has_file(self):
        missing = [s for s in menu_slugs() if not (DOCS_DIR / f'{s}.md').exists()]
        self.assertEqual(missing, [], f'нет файлов документации для страниц меню: {missing}')

    def test_no_orphan_files(self):
        orphans = sorted(doc_files() - set(menu_slugs()))
        self.assertEqual(orphans, [], f'файлы вне меню /docs (о них нельзя узнать): {orphans}')

    def test_internal_links_are_valid(self):
        slugs = set(menu_slugs())
        broken = []
        for path in sorted(DOCS_DIR.glob('*.md')):
            text = path.read_text(encoding='utf-8')
            for target in re.findall(r'\]\(/docs/([a-z0-9_-]+)\)', text):
                if target not in slugs:
                    broken.append(f'{path.name} → /docs/{target}')
        self.assertEqual(broken, [], f'битые ссылки: {broken}')

    def test_pages_render_without_markdown_leftovers(self):
        checks = ((r'\*\*', 'незакрытая жирная разметка **'), (r'`', 'инлайн-код не отрендерился'),
                  (r'\]\(', 'ссылка не отрендерилась'), (r'(?m)^#{1,6} ', 'заголовок не отрендерился'),
                  (r'(?m)^\s*[-*+] ', 'пункт списка не отрендерился'))
        for path in sorted(DOCS_DIR.glob('*.md')):
            html = docs_renderer.render(path.read_text(encoding='utf-8'))
            self.assertTrue(html.strip(), f'{path.name}: страница отрендерилась пустой')
            self.assertIn('<h1', html, f'{path.name}: нет заголовка первого уровня')
            for pattern, description in checks:
                self.assertFalse(re.search(pattern, html), f'{path.name}: {description}')

    def test_site_page_lists_limits_and_update(self):
        """Страница про сайт обязана объяснять лимит страниц, обновление и ограничения."""
        text = (DOCS_DIR / 'site.md').read_text(encoding='utf-8')
        for expected in ('Индексировать сайт', 'Обновить с сайта', 'robots.txt', 'JavaScript',
                         'лимит страниц', '10 000 строк'):
            self.assertIn(expected, text, f'в docs/site.md нет упоминания: {expected}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
