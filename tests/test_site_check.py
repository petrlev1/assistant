# test_site_check.py — проверка изменений на сайте по карте (lastmod), без обхода страниц
"""Проверки site_crawler.check_site_changes и разбора карты сайта (_sitemap_entries).

Проверяется: сравнение lastmod из карты с моментом нашей выборки, новые и пропавшие
страницы, отнесение страниц к файлам по разделам, молчание без карты сайта и пометка
источника страницы (из карты или по ссылке) во время обхода.

Запуск:  venv/Scripts/python.exe tests/test_site_check.py
         (Linux-сервер: venv/bin/python tests/test_site_check.py)
Работает в отдельном временном каталоге: файлы реальных пользователей не затрагиваются.
"""

import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import site_crawler as sc

TEST_USER = 990004
DOMAIN = "127.0.0.1"
SITEMAP_PATH = "/sitemap.xml"
SUBMAP_PATH = "/sitemap-iblock-1.xml"

OLD = "2026-09-01T10:00:00+03:00"       # карта: страница старая
NEW = "2026-09-30T10:00:00+03:00"       # карта: страница новее нашей выборки
FETCHED = "2026-09-20T12:00:00"         # когда мы её забирали (время без зоны = местное)


def page_html(title, links=()):
    items = "".join(f'<a href="{href}">ссылка</a>' for href in links)
    return (f"<html><head><title>{title}</title></head><body>"
            f"<h1>{title}</h1><p>Описание страницы {title} достаточно длинное для извлечения.</p>"
            f"{items}</body></html>")


class Site(http.server.BaseHTTPRequestHandler):
    """Локальный сайт: карта сайта, robots и несколько страниц."""

    routes = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        entry = self.routes.get(path)
        if not entry:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = entry
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class CheckCase(unittest.TestCase):
    """check_site_changes на карте сайта с lastmod."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_site_check_")
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        os.environ["SITE_CRAWLER_ALLOW_LOCAL"] = "1"
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Site)
        cls.server.allow_reuse_address = True
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.build_routes()

    @classmethod
    def build_routes(cls):
        cls.robots = f"User-agent: *\nAllow: /\nSitemap: {cls.base}{SITEMAP_PATH}\n"
        cls.index = ('<?xml version="1.0" encoding="UTF-8"?><sitemapindex>'
                     f'<sitemap><loc>{cls.base}{SUBMAP_PATH}</loc>'
                     f'<lastmod>{NEW}</lastmod></sitemap></sitemapindex>')
        # a/1 — изменилась, a/2 — без изменений, b/1 — новая для нас; a/3 в карте уже нет
        cls.submap = ('<?xml version="1.0" encoding="UTF-8"?><urlset>'
                      f'<url><loc>{cls.base}/catalog/a/1.html</loc><lastmod>{NEW}</lastmod></url>'
                      f'<url><loc>{cls.base}/catalog/a/2.html</loc><lastmod>{OLD}</lastmod></url>'
                      f'<url><loc>{cls.base}/catalog/b/1.html</loc><lastmod>{OLD}</lastmod></url>'
                      f'<url><loc>{cls.base}/catalog/b/2.html</loc><lastmod>{OLD}</lastmod></url>'
                      f'<url><loc>{cls.base}/catalog/a/</loc><lastmod>{OLD}</lastmod></url>'
                      '</urlset>')
        Site.routes = {
            "/robots.txt": ("text/plain", cls.robots),
            SITEMAP_PATH: ("application/xml", cls.index),
            SUBMAP_PATH: ("application/xml", cls.submap),
            "/catalog/a/": ("text/html", page_html("Раздел A", ["/catalog/a/1.html"])),
            "/catalog/a/1.html": ("text/html", page_html("Страница A1", ["/catalog/a/linked.html"])),
            "/catalog/a/2.html": ("text/html", page_html("Страница A2")),
            # карта отдаёт раздел a с хвостовым слэшем, в манифесте он без слэша
            "/catalog/a": ("text/html", page_html("Раздел A без слэша")),
            "/catalog/a/linked.html": ("text/html", page_html("Ссылочная страница")),
            "/catalog/b/1.html": ("text/html", page_html("Страница B1")),
        }

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.environ.pop("SITE_CRAWLER_ALLOW_LOCAL", None)
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        folder = sc._site_dir(TEST_USER, DOMAIN)
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(os.path.join("Database", f"user_{TEST_USER}"), ignore_errors=True)
        self.build_routes()
        # Манифесты двух файлов БЗ домена (по одному на раздел)
        sc._save_manifest(TEST_USER, DOMAIN, {
            "start_url": self.base + "/catalog/a/", "domain": DOMAIN,
            "filename": sc.site_filename(DOMAIN, "/catalog/a"), "section": "/catalog/a",
            "lines": 4, "pages": {
                self.base + "/catalog/a/1.html": {"title": "A1", "lines": [], "fetched_at": FETCHED,
                                                  "source": "sitemap"},
                self.base + "/catalog/a/2.html": {"title": "A2", "lines": [], "fetched_at": FETCHED,
                                                  "source": "sitemap"},
                self.base + "/catalog/a/3.html": {"title": "A3", "lines": [], "fetched_at": FETCHED,
                                                  "source": "sitemap"},
                self.base + "/catalog/a/linked.html": {"title": "L", "lines": [], "fetched_at": FETCHED,
                                                       "source": "link"},
                # тот же адрес, что в карте отдан с хвостовым слэшем (см. build_routes)
                self.base + "/catalog/a": {"title": "Раздел A", "lines": [], "fetched_at": FETCHED,
                                           "source": "sitemap"},
            }}, "/catalog/a")
        sc._save_manifest(TEST_USER, DOMAIN, {
            "start_url": self.base + "/catalog/b/", "domain": DOMAIN,
            "filename": sc.site_filename(DOMAIN, "/catalog/b"), "section": "/catalog/b",
            "lines": 1, "pages": {
                self.base + "/catalog/b/2.html": {"title": "B2", "lines": [], "fetched_at": FETCHED,
                                                  "source": "sitemap"},
            }}, "/catalog/b")

    def test_report_counts_new_changed_gone(self):
        """Отчёт: изменённая, новая и пропавшая страницы — по своим файлам."""
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=True)
        self.assertTrue(report["ok"])
        self.assertTrue(report["supported"])
        self.assertEqual(report["sitemap_urls"], 5)
        self.assertEqual(report["known_urls"], 6)
        self.assertEqual(report["unknown"], 0)
        self.assertEqual(report["new"], 1)          # /catalog/b/1.html
        self.assertEqual(report["changed"], 1)      # /catalog/a/1.html (lastmod новее выборки)
        self.assertEqual(report["gone"], 1)         # /catalog/a/3.html исчезла из карты
        self.assertEqual(report["files_total"], 2)
        self.assertEqual(report["files_affected"], 2)
        by_section = {f["section"]: f for f in report["files"]}
        self.assertEqual(by_section["/catalog/a"]["changed"], 1)
        self.assertEqual(by_section["/catalog/a"]["gone"], 1)
        self.assertEqual(by_section["/catalog/a"]["new"], 0)
        self.assertEqual(by_section["/catalog/b"]["new"], 1)
        self.assertEqual(by_section["/catalog/a"]["sample_gone"], ["/catalog/a/3.html"])
        self.assertEqual(by_section["/catalog/a"]["sample_changed"], ["/catalog/a/1.html"])

    def test_link_pages_are_not_reported_gone(self):
        """Страница, найденная по ссылке (её в карте нет), пропавшей не считается."""
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=False)
        self.assertEqual(report["gone"], 1)
        all_gone = [p for f in report["files"] for p in f["sample_gone"]]
        self.assertNotIn("/catalog/a/linked.html", all_gone)

    def test_without_sitemap_reports_unsupported(self):
        """Карты сайта нет — проверка честно говорит, что не может сравнить."""
        Site.routes.pop(SITEMAP_PATH, None)
        Site.routes.pop(SUBMAP_PATH, None)
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=False)
        self.assertTrue(report["ok"])
        self.assertFalse(report["supported"])
        self.assertEqual(report["reason"], "no_sitemap")
        self.assertEqual(report["new"], 0)

    def test_sitemap_entries_parses_lastmod(self):
        """Разбор карты: адреса и lastmod, включая вложенную карту из индекса."""
        import requests
        entries, read = sc._sitemap_entries(requests.Session(), self.base + "/catalog/", [])
        self.assertEqual(read, 2)
        self.assertEqual(entries[self.base + "/catalog/a/1.html"], NEW)
        self.assertEqual(entries[self.base + "/catalog/a/2.html"], OLD)

    def test_trailing_slash_is_not_a_new_page(self):
        """Адрес в карте с хвостовым слэшем не делает страницу новой (и наоборот)."""
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=False)
        by_section = {f["section"]: f for f in report["files"]}
        self.assertEqual(by_section["/catalog/a"]["new"], 0, "раздел a посчитан новым из-за слэша")
        self.assertEqual(sc.url_key(self.base + "/catalog/a/"), sc.url_key(self.base + "/catalog/a"))
        self.assertEqual(sc.url_key("HTTPS://Aquasegment.RU/Catalog/"), "https://aquasegment.ru/Catalog")

    def test_sitemap_without_lastmod_is_not_a_clean_bill(self):
        """Карта без lastmod: отчёт обязан сказать, что сравнивать нечем."""
        Site.routes[SUBMAP_PATH] = ("application/xml",
                                    '<?xml version="1.0" encoding="UTF-8"?><urlset>'
                                    f'<url><loc>{self.base}/catalog/a/1.html</loc></url>'
                                    '</urlset>')
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=False)
        self.assertFalse(report["supported"])
        self.assertEqual(report["reason"], "no_lastmod")
        self.assertEqual(report["changed"], 0)

    def test_pages_without_lastmod_are_counted(self):
        """Страница в карте без lastmod попадает в «неизвестно», а не в «всё хорошо»."""
        Site.routes[SUBMAP_PATH] = ("application/xml",
                                    '<?xml version="1.0" encoding="UTF-8"?><urlset>'
                                    f'<url><loc>{self.base}/catalog/a/1.html</loc><lastmod>{NEW}</lastmod></url>'
                                    f'<url><loc>{self.base}/catalog/a/2.html</loc></url>'
                                    '</urlset>')
        report = sc.check_site_changes(TEST_USER, self.base + "/catalog/", respect_robots=False)
        self.assertTrue(report["supported"])
        self.assertEqual(report["changed"], 1)
        self.assertEqual(report["unknown"], 1)

    def test_crawl_marks_page_source(self):
        """Обход помечает, откуда узнал страницу: из карты сайта или по ссылке."""
        result = sc.crawl(TEST_USER, self.base + "/catalog/a/", page_limit=10, respect_robots=True,
                          section=True)
        manifest = sc._load_manifest(TEST_USER, DOMAIN, "/catalog/a")
        pages = manifest.get("pages") or {}
        self.assertIn(self.base + "/catalog/a/1.html", pages)
        self.assertEqual(pages[self.base + "/catalog/a/1.html"]["source"], "sitemap")
        self.assertEqual(pages[self.base + "/catalog/a/linked.html"]["source"], "link")
        self.assertGreater(result["lines"], 0)


class MomentCase(unittest.TestCase):
    """Сравнение времени из карты сайта и манифеста."""

    def test_newer_lastmod_means_changed(self):
        self.assertTrue(sc._page_is_newer(NEW, FETCHED))
        self.assertFalse(sc._page_is_newer(OLD, FETCHED))

    def test_naive_and_zoned_times_are_comparable(self):
        """Карта отдаёт время с зоной, манифест — местное: сравнивать можно."""
        local_now = datetime.now().isoformat(timespec="seconds")
        self.assertFalse(sc._page_is_newer("2020-01-01T00:00:00+03:00", local_now))
        self.assertTrue(sc._page_is_newer("2030-01-01T00:00:00+03:00", local_now))

    def test_unparsable_or_missing_times_are_rechecked(self):
        self.assertTrue(sc._page_is_newer("не дата", FETCHED))
        # Пустой lastmod — карта о странице молчит: это не «изменилась», но такие
        # страницы отчёт считает отдельно (поле unknown)
        self.assertFalse(sc._page_is_newer("", FETCHED))
        self.assertFalse(sc._page_is_newer("", ""))

    def test_in_section_scope(self):
        self.assertTrue(sc.in_section("https://s.ru/catalog/a/1.html", "https://s.ru/catalog/", "/catalog/a"))
        self.assertFalse(sc.in_section("https://s.ru/catalog/b/1.html", "https://s.ru/catalog/", "/catalog/a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
