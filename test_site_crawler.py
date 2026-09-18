# test_site_crawler.py — проверки импорта сайта (модуль site_crawler.py)
"""Тесты краулера: нормализация URL, защита от SSRF, разбор HTML, сборка строк-фактов,
инкрементальный обход (304) и полный прогон по локальному тестовому сайту.

Запуск:  venv/Scripts/python.exe test_site_crawler.py
Тесты работают в отдельном временном каталоге (там же создаются site_cache/ и Database/),
поэтому файлы реальных пользователей не затрагиваются.
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
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import site_crawler as sc

PAGE_LIMIT = 10
TEST_USER = 990001

SITE = {
    "index.html": """<!doctype html><html><head><title>Термочехлы AQ — производство</title>
        <meta name="description" content="Производим термочехлы для промышленного оборудования и трубопроводов.">
        </head><body>
        <div class="menu"><a href="/about.html">О нас</a><a href="/contacts.html">Контакты</a></div>
        <nav><a href="/about.html">Навигация</a></nav>
        <h1>Термочехлы для промышленного оборудования</h1>
        <p>Термочехлы AQ изготовлены из многослойного материала на основе хлоропренового каучука и служат до 10 лет.</p>
        <h2>Съёмные термочехлы</h2>
        <p>Работаем по всей России: срок изготовления 5 дней.</p>
        <p>Съёмные термочехлы устанавливаются на оборудование без его демонтажа и не требуют специальных инструментов для монтажа.</p>
        <div class="footer">© 2026 Термочехлы AQ, тел. +7 000 000-00-00</div>
        <script>var junk = 'этот текст не должен попасть в базу знаний';</script>
        </body></html>""",
    "about.html": """<html><head><title>О компании</title></head><body>
        <h1>О компании</h1>
        <p>Компания работает на рынке теплоизоляции с 2012 года и выпускает более 2000 видов термочехлов в год.</p>
        <p>Короткий абзац.</p>
        <a href="/index.html">На главную</a></body></html>""",
    "contacts.html": """<html><head><title>Контакты</title></head><body>
        <p>Офис находится в Москве, производство — в Московской области, отгрузка со склада в течение суток.</p>
        </body></html>""",
    "private/secret.html": "<html><head><title>Секрет</title></head><body><p>Эта страница закрыта в robots.txt и не должна попасть в файл базы знаний никогда.</p></body></html>",
    "sitemap.xml": """<?xml version="1.0" encoding="UTF-8"?>
        <urlset><url><loc>{base}/about.html</loc></url><url><loc>{base}/contacts.html</loc></url></urlset>""",
    "robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n",
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):        # тишина в выводе тестов
        pass


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class CrawlerCase(unittest.TestCase):
    """Общий временный рабочий каталог: относительные site_cache/ и Database/ идут туда."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_site_test_")
        cls.site_dir = os.path.join(cls.tmp, "site_src")
        os.makedirs(os.path.join(cls.site_dir, "private"), exist_ok=True)
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)

        handler = lambda *a, **kw: _Handler(*a, directory=cls.site_dir, **kw)
        cls.server = _ThreadedServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        for name, content in SITE.items():
            with open(os.path.join(cls.site_dir, name), "w", encoding="utf-8") as f:
                f.write(content.replace("{base}", cls.base))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        os.environ[sc.ALLOW_LOCAL_ENV] = "1"       # обход localhost разрешён только в тестах

    def tearDown(self):
        """Ждём завершения фонового обхода и сбрасываем состояние джоба (тесты независимы)."""
        deadline = time.time() + 30
        while time.time() < deadline and sc._JOB["active"]:
            time.sleep(0.2)
        with sc._JOB_LOCK:
            sc._JOB.update(active=False, job=None, finished_at=0.0)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop(sc.ALLOW_LOCAL_ENV, None)
        cls.server.shutdown()
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestDecoding(unittest.TestCase):
    """Кодировка страниц: charset из заголовка, из <meta>, иначе utf-8 → cp1251."""

    class _Response:
        def __init__(self, content_type):
            self.headers = {"Content-Type": content_type}

    def test_utf8_without_charset(self):
        body = "Термочехлы для трубопроводов".encode("utf-8")
        self.assertEqual(sc._decode(self._Response("text/html"), body), "Термочехлы для трубопроводов")

    def test_cp1251_from_header(self):
        body = "Термочехлы для трубопроводов".encode("cp1251")
        self.assertEqual(sc._decode(self._Response("text/html; charset=windows-1251"), body),
                         "Термочехлы для трубопроводов")

    def test_charset_from_meta(self):
        html = '<html><head><meta charset="windows-1251"><title>х</title></head><body>Термочехлы</body></html>'
        self.assertIn("Термочехлы", sc._decode(self._Response("text/html"), html.encode("cp1251")))


class TestUrlRules(CrawlerCase):
    def test_normalize_url(self):
        self.assertEqual(sc.normalize_url("HTTPS://Example.com:443/a/b/?utm_source=x&y=1#top"),
                         "https://example.com/a/b?y=1")
        self.assertEqual(sc.normalize_url("http://example.com"), "http://example.com/")
        self.assertEqual(sc.normalize_url("http://example.com:8080/a/"), "http://example.com:8080/a")
        self.assertEqual(sc.normalize_url("не-url"), "")

    def test_same_host_and_domain(self):
        self.assertTrue(sc.same_host("https://www.example.com/a", "http://example.com/b"))
        self.assertFalse(sc.same_host("https://example.com", "https://other.org"))
        self.assertEqual(sc.domain_of("https://www.example.com/x"), "example.com")

    def test_ssrf_blocked(self):
        os.environ.pop(sc.ALLOW_LOCAL_ENV, None)   # проверяем боевое поведение
        try:
            for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/",
                        "http://127.0.0.1:8077/", "http://localhost:8077/",
                        "http://10.0.0.5/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
                        "http://[::1]/", "http://0.0.0.0/"):
                with self.assertRaises(sc.CrawlError, msg=f"должен быть отклонён: {url}"):
                    sc.assert_public_url(url)
        finally:
            os.environ[sc.ALLOW_LOCAL_ENV] = "1"

    def test_ssrf_allows_local_in_tests_only(self):
        sc.assert_public_url("http://127.0.0.1:1234/")     # с включённым флагом — можно


class TestHtmlParsing(CrawlerCase):
    def test_boilerplate_and_scripts_are_stripped(self):
        html = SITE["index.html"].replace("{base}", "https://example.com")
        title, description, blocks, links = sc.extract_page(html)
        text = " ".join(t for _tag, t in blocks)
        self.assertIn("Термочехлы", title)
        self.assertIn("Производим термочехлы", description)
        self.assertNotIn("не должен попасть в базу знаний", text)     # <script>
        self.assertNotIn("тел. +7", text)                             # <div class="footer">
        self.assertNotIn("Навигация", text)                           # <nav>
        self.assertNotIn("О нас", text)                               # <div class="menu">
        self.assertIn("хлоропренового каучука", text)
        self.assertIn("/about.html", links)

    def test_links_skip_service_schemes(self):
        html = '<a href="mailto:a@b.c">почта</a><a href="javascript:void(0)">js</a><a href="/ok.html">ok</a>'
        _t, _d, _b, links = sc.extract_page(html)
        self.assertEqual(links, ["/ok.html"])

    def test_short_fact_gets_section_context(self):
        blocks = [("h2", "Условия доставки термочехлов по России"), ("p", "24 часа.")]
        lines = sc.html_to_lines("Термочехлы AQ", "", blocks, set())
        self.assertEqual(lines, ["Условия доставки термочехлов по России: 24 часа."])

    def test_navigation_junk_is_dropped(self):
        blocks = [("p", "Главная"), ("p", "Контакты"), ("p", "О нас"), ("p", "Читать далее")]
        self.assertEqual(sc.html_to_lines("AQ", "", blocks, set()), [])

    def test_long_paragraph_split_and_global_dedupe(self):
        long_text = ("Термочехлы подходят для трубопроводов, ёмкостей и насосов. " * 6).strip()
        seen = set()
        first = sc.html_to_lines("Стр", "", [("p", long_text)], seen)
        second = sc.html_to_lines("Стр", "", [("p", long_text)], seen)     # тот же текст на другой странице
        self.assertGreater(len(first), 1)
        self.assertTrue(all(len(line) <= sc.MAX_LINE_CHARS for line in first))
        self.assertEqual(second, [])

    def test_split_block_hard_wrap(self):
        pieces = sc.split_block("слово " * 200)
        self.assertTrue(all(len(p) <= sc.MAX_LINE_CHARS for p in pieces))


class TestRobots(CrawlerCase):
    def test_prefix_rules_and_allow_precedence(self):
        disallow = ["/private/", "/"]
        allow = ["/private/open/"]
        self.assertFalse(sc._robots_allowed("http://x.test/private/a", disallow, allow))
        self.assertTrue(sc._robots_allowed("http://x.test/private/open/a", disallow, allow))
        self.assertFalse(sc._robots_allowed("http://x.test/any", disallow, allow))


class TestFullCrawl(CrawlerCase):
    def _reset_site(self):
        """Стираем состояние тестового сайта (кэш + файл БЗ) — каждый тест с чистого листа."""
        shutil.rmtree(os.path.join(sc.CACHE_ROOT, f"user_{TEST_USER}"), ignore_errors=True)
        shutil.rmtree(os.path.join("Database", f"user_{TEST_USER}"), ignore_errors=True)

    def setUp(self):
        self._reset_site()

    def _read_txt(self):
        path = os.path.join("Database", f"user_{TEST_USER}", sc.site_filename("127.0.0.1"))
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_crawl_builds_knowledge_file(self):
        result = sc.crawl(TEST_USER, self.base + "/index.html", PAGE_LIMIT)
        text = self._read_txt()
        self.assertEqual(result["filename"], "site_127.0.0.1.txt")
        self.assertEqual(result["stats"]["new"], 3)                  # index + about + contacts
        self.assertNotIn("Секрет", text)                             # robots.txt: /private/ закрыт
        self.assertNotIn("тел. +7", text)
        self.assertIn("хлоропренового каучука", text)
        self.assertIn("# Термочехлы AQ — производство", text)         # разделитель страницы
        self.assertIn("с 2012 года", text)                            # факт со второй страницы
        lines = [line for line in text.splitlines() if line.strip()]
        facts = [line for line in lines if not line.startswith("#")]
        self.assertTrue(facts)
        self.assertTrue(all(len(f) <= sc.MAX_LINE_CHARS for f in facts))
        # сортировка/порядок: главная идёт первой
        self.assertTrue(lines[0].startswith("#"))
        manifest = sc._load_manifest(TEST_USER, "127.0.0.1")
        self.assertEqual(manifest["start_url"], self.base + "/index.html")
        self.assertEqual(len(manifest["pages"]), 3)
        self.assertTrue(os.path.isdir(os.path.join(sc.CACHE_ROOT, f"user_{TEST_USER}", "127.0.0.1", "html")))

    def _full_crawl(self, limit=PAGE_LIMIT):
        """Полный обход «как есть» — исходное состояние: 3 страницы в файле."""
        return sc.crawl(TEST_USER, self.base + "/index.html", limit)

    def test_recrawl_keeps_pages_on_304(self):
        """Ответ 304 по части страниц: текст берётся из кэша, остальные не теряются."""
        self._full_crawl()
        real_fetch = sc.fetch
        calls = {"304": 0}

        def fake_fetch(session, url, etag="", last_modified="", expect_html=True):
            if url.endswith(("about.html", "contacts.html")):
                calls["304"] += 1
                return {"url": url, "status": 304, "html": None, "content_type": "",
                        "etag": "", "last_modified": ""}
            # главную перекачиваем без условных заголовков — как будто она изменилась
            return real_fetch(session, url, "", "", expect_html)

        sc.fetch = fake_fetch
        try:
            result = sc.crawl(TEST_USER, self.base + "/index.html", PAGE_LIMIT)
        finally:
            sc.fetch = real_fetch
        self.assertEqual(calls["304"], 2)
        self.assertEqual(result["stats"]["unchanged"], 2)
        self.assertEqual(result["stats"]["changed"], 1)
        self.assertEqual(result["stats"]["gone"], 0)
        text = self._read_txt()
        self.assertIn("с 2012 года", text)                       # текст из кэша 304-страницы
        self.assertIn("Офис находится в Москве", text)
        manifest = sc._load_manifest(TEST_USER, "127.0.0.1")
        self.assertEqual(len(manifest["pages"]), 3)

    def test_page_removed_from_site_leaves_kb(self):
        """Страница, которой больше нет (404), уходит из файла."""
        self._full_crawl()
        real_fetch = sc.fetch

        def fake_fetch(session, url, etag="", last_modified="", expect_html=True):
            if url.endswith("contacts.html"):
                return {"url": url, "status": 404, "html": None, "content_type": "",
                        "etag": "", "last_modified": ""}
            return real_fetch(session, url, etag, last_modified, expect_html)

        sc.fetch = fake_fetch
        try:
            result = sc.crawl(TEST_USER, self.base + "/index.html", PAGE_LIMIT)
        finally:
            sc.fetch = real_fetch
        self.assertEqual(result["stats"]["gone"], 1)
        self.assertNotIn("Офис находится в Москве", self._read_txt())
        self._full_crawl()                       # возвращаем состояние для остальных проверок

    def test_page_limit_does_not_drop_old_pages(self):
        """Жёсткий лимит страниц не выбрасывает остальные страницы из файла."""
        self._full_crawl()
        result = sc.crawl(TEST_USER, self.base + "/index.html", 1)
        self.assertEqual(result["stats"]["pages"], 1)
        self.assertGreaterEqual(result["stats"]["carried"], 2)
        text = self._read_txt()
        self.assertIn("с 2012 года", text)
        self.assertIn("Офис находится в Москве", text)

    def test_cancel_keeps_previous_content(self):
        """Отмена обхода сохраняет ранее собранные страницы в файле."""
        self._full_crawl()
        event = threading.Event()
        result = sc.crawl(TEST_USER, self.base + "/index.html", PAGE_LIMIT,
                          on_progress=lambda state: (event.set() if state["pages"] >= 1 else None),
                          cancel_event=event)
        self.assertTrue(result["stats"]["cancelled"])
        self.assertGreaterEqual(result["stats"]["carried"], 1)
        self.assertIn("с 2012 года", self._read_txt())

    def test_url_already_running_or_bad_host(self):
        os.environ.pop(sc.ALLOW_LOCAL_ENV, None)   # боевой режим: приватные адреса запрещены
        try:
            with self.assertRaises(sc.CrawlError):
                sc.start_job(TEST_USER, "ftp://example.com/")
            with self.assertRaises(sc.CrawlError):
                sc.start_job(TEST_USER, "http://169.254.169.254/")
        finally:
            os.environ[sc.ALLOW_LOCAL_ENV] = "1"


class TestJobManager(CrawlerCase):
    def test_job_lifecycle_and_status_json(self):
        done = threading.Event()
        seen = []

        def on_finish(user_id, result):
            seen.append(result["filename"])

        job = sc.start_job(TEST_USER, self.base + "/index.html", 3, True,
                           on_finish=lambda uid, res: (on_finish(uid, res), done.set()))
        self.assertTrue(job["url"].startswith("http://127.0.0.1"))
        with self.assertRaises(sc.CrawlError):      # второй обход параллельно нельзя
            sc.start_job(TEST_USER, self.base + "/about.html", 3)
        self.assertTrue(done.wait(60), "обход не завершился за 60 с")
        state = sc.status_json()
        self.assertFalse(state["active"])
        self.assertEqual(state["job"]["phase"], "index")
        self.assertTrue(state["job"]["indexed"])
        self.assertEqual(seen, ["site_127.0.0.1.txt"])
        self.assertNotIn("_cancel_event", json.dumps(state["job"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
