# test_site_sections.py — режим «только этот раздел» (site_crawler: section=True)
"""Проверки постраничной индексации раздела: адрес вида https://site.ru/catalog/
обходит только страницы этого раздела и пишет их в отдельный файл базы знаний.

Что именно проверяется:
* префикс пути из адреса и границы раздела (/catalog не захватывает /catalog-sale
  и /catalogovyj);
* имя файла БЗ для раздела и отдельный манифест обхода (иначе обход раздела затирал
  бы манифест обхода всего сайта);
* sitemap.xml тоже фильтруется по разделу;
* файл раздела и файл всего сайта существуют одновременно и не смешиваются;
* повторный обход раздела не подхватывает страницы всего сайта.

Работа идёт в отдельном временном каталоге (site_cache/ и Database/ создаются там же),
локальный тестовый сайт поднимается на 127.0.0.1 — реальные сайты не затрагиваются.

Запуск:  venv/Scripts/python.exe tests/test_site_sections.py   (Linux: venv/bin/python tests/test_site_sections.py)
"""

import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import auth_db / rag_core / web_app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_crawler as sc  # noqa: E402

TEST_USER = 990002

# Небольшой сайт с разделом каталога, похожим на реальный (Bitrix-каталог):
# внутри раздела есть подраздел и карточка, рядом — «похожие» пути-ловушки.
SITE = {
    "index.html": """<html><head><title>Главная страница компании</title></head><body>
        <p>Мы поставляем оборудование для водоподготовки по всей России и работаем с 2008 года.</p>
        <a href="/catalog/index.html">Каталог</a><a href="/about.html">О компании</a>
        </body></html>""",
    "about.html": """<html><head><title>О компании</title></head><body>
        <p>Компания основана в 2008 году, склад находится в Москве, отгрузка идёт со склада в течение суток.</p>
        </body></html>""",
    "catalogovyj.html": """<html><head><title>Страница с похожим адресом</title></head><body>
        <p>Эта страница лежит в корне сайта и не относится к разделу каталога, хотя адрес начинается похоже.</p>
        </body></html>""",
    "catalog-sale/index.html": """<html><head><title>Распродажа со склада</title></head><body>
        <p>Ликвидация складских остатков: скидки на фильтры и комплектующие до конца месяца.</p>
        </body></html>""",
    "catalog/index.html": """<html><head><title>Каталог оборудования для водоподготовки</title></head><body>
        <p>В каталоге собраны системы водоочистки, комплектующие для систем и термочехлы для оборудования.</p>
        <a href="/catalog/aeratsiya/index.html">Аэрация</a>
        <a href="/catalog-sale/index.html">Распродажа</a>
        <a href="/about.html">О компании</a>
        <a href="/catalogovyj.html">Похожий адрес</a>
        </body></html>""",
    "catalog/aeratsiya/index.html": """<html><head><title>Аэрация воды — комплектующие</title></head><body>
        <p>Аэрационные колонны удаляют из воды железо и сероводород, подбираются по расходу воды в час.</p>
        <a href="/catalog/aeratsiya/filter.html">Фильтр-аэратор</a>
        </body></html>""",
    "catalog/aeratsiya/filter.html": """<html><head><title>Фильтр-аэратор для воды</title></head><body>
        <p>Фильтр-аэратор со встроенным эжектором работает без реагентов и насоса при давлении от двух атмосфер.</p>
        </body></html>""",
    "sitemap.xml": """<?xml version="1.0" encoding="UTF-8"?>
        <urlset><url><loc>{base}/index.html</loc></url><url><loc>{base}/about.html</loc></url>
        <url><loc>{base}/catalog/index.html</loc></url><url><loc>{base}/catalog/aeratsiya/index.html</loc></url>
        <url><loc>{base}/catalog-sale/index.html</loc></url></urlset>""",
    "robots.txt": "User-agent: *\nSitemap: {base}/sitemap.xml\n",
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):        # тишина в выводе тестов
        pass


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SectionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_section_test_")
        cls.site_dir = os.path.join(cls.tmp, "site_src")
        os.makedirs(os.path.join(cls.site_dir, "catalog", "aeratsiya"), exist_ok=True)
        os.makedirs(os.path.join(cls.site_dir, "catalog-sale"), exist_ok=True)
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
        os.environ[sc.ALLOW_LOCAL_ENV] = "1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- утилиты ------------------------------------------------------------
    def txt_path(self, filename):
        return Path("Database") / f"user_{TEST_USER}" / filename

    def read_txt(self, filename):
        return self.txt_path(filename).read_text(encoding="utf-8")

    def crawl_section(self, url=None, limit=10, **kwargs):
        return sc.crawl(TEST_USER, url or f"{self.base}/catalog/", limit,
                        respect_robots=kwargs.pop("respect_robots", False),
                        section=True, **kwargs)

    def crawl_site(self, url=None, limit=10, **kwargs):
        return sc.crawl(TEST_USER, url or f"{self.base}/", limit,
                        respect_robots=kwargs.pop("respect_robots", False), **kwargs)

    def setUp(self):
        """Каждый тест начинает с чистого кэша и чистых файлов БЗ."""
        for path in (Path("site_cache"), Path("Database")):
            shutil.rmtree(path, ignore_errors=True)


class TestSectionRules(SectionCase):
    def test_section_prefix_from_url(self):
        self.assertEqual(sc.section_prefix("https://site.ru/catalog/"), "/catalog")
        self.assertEqual(sc.section_prefix("https://site.ru/catalog"), "/catalog")
        self.assertEqual(sc.section_prefix("https://site.ru/catalog/termostaty/"), "/catalog/termostaty")
        self.assertEqual(sc.section_prefix("https://site.ru/"), "")
        self.assertEqual(sc.section_prefix("https://site.ru"), "")

    def test_section_filename(self):
        self.assertEqual(sc.site_filename("example.com"), "site_example.com.txt")
        self.assertEqual(sc.site_filename("example.com", "/catalog"), "site_example.com_catalog.txt")
        self.assertEqual(sc.site_filename("example.com", "/catalog/aeratsiya"),
                         "site_example.com_catalog_aeratsiya.txt")

    def test_long_section_gets_short_hash_name(self):
        long_prefix = "/catalog/" + "/".join(f"razdel{index}" for index in range(20))
        other_prefix = "/catalog/" + "/".join(f"razdel{index}" for index in range(20)) + "/hvost"
        name_one = sc.site_filename("example.com", long_prefix)
        name_two = sc.site_filename("example.com", other_prefix)
        # Имя не растёт бесконечно: слаг обрезан, хвост — хэш от полного пути.
        self.assertLessEqual(len(name_one), 90, f"слишком длинное имя файла: {name_one}")
        self.assertTrue(name_one.endswith(".txt"))
        self.assertNotEqual(name_one, name_two, "два разных раздела получили одно имя файла")

    def test_redirect_keeps_trailing_slash(self):
        """Без этого /catalog уходил в вечный редирект (сервер отвечает 301 на /catalog/)."""
        self.assertEqual(sc.normalize_redirect("https://site.ru/catalog/"), "https://site.ru/catalog/")
        self.assertEqual(sc.normalize_redirect("https://site.ru/catalog"), "https://site.ru/catalog")
        self.assertEqual(sc.normalize_redirect("https://site.ru/catalog/?utm_source=yandex"),
                         "https://site.ru/catalog/")
        self.assertEqual(sc.normalize_redirect("/catalog/"), "")

    def test_in_section_boundaries(self):
        base = "https://site.ru"
        start = base + "/catalog/"
        self.assertTrue(sc.in_section(base + "/catalog", start, "/catalog"))
        self.assertTrue(sc.in_section(base + "/catalog/aeratsiya/filter", start, "/catalog"))
        self.assertFalse(sc.in_section(base + "/catalog-sale", start, "/catalog"))
        self.assertFalse(sc.in_section(base + "/catalogovyj", start, "/catalog"))
        self.assertFalse(sc.in_section(base + "/about", start, "/catalog"))
        self.assertFalse(sc.in_section("https://other.ru/catalog/a", start, "/catalog"))
        # без префикса — весь сайт
        self.assertTrue(sc.in_section(base + "/about", start, ""))


class TestSectionCrawl(SectionCase):
    def test_section_crawl_writes_only_section_pages(self):
        result = self.crawl_section()

        self.assertEqual(result["section"], "/catalog")
        self.assertEqual(result["filename"], "site_127.0.0.1_catalog.txt")
        text = self.read_txt(result["filename"])

        self.assertIn("Аэрационные колонны", text)
        self.assertIn("Фильтр-аэратор", text)
        self.assertIn("В каталоге собраны системы водоочистки", text)
        # ничего из-за пределов раздела
        self.assertNotIn("Компания основана в 2008 году", text)
        self.assertNotIn("Ликвидация складских остатков", text)
        self.assertNotIn("адрес начинается похоже", text)

        # sitemap.xml отдал адреса всего сайта — фильтр раздела должен их отсечь
        self.assertNotIn(f"{self.base}/about.html", text)
        self.assertEqual(result["stats"]["errors"], 0, f"ошибки обхода: {result['stats']}")

    def test_directory_url_without_slash_is_crawled(self):
        """Адрес раздела — каталог: и со слэшем, и без него страница должна обходиться."""
        without_slash = self.crawl_section(url=f"{self.base}/catalog")
        with_slash = self.crawl_section(url=f"{self.base}/catalog/")

        for result in (without_slash, with_slash):
            self.assertEqual(result["stats"]["errors"], 0,
                             f"страница-каталог ушла в ошибку: {result['stats']}")
            self.assertIn("В каталоге собраны системы водоочистки", self.read_txt(result["filename"]))

    def test_section_has_its_own_manifest(self):
        self.crawl_section()
        section_manifest = sc._load_manifest(TEST_USER, "127.0.0.1", "/catalog")
        self.assertEqual(section_manifest.get("section"), "/catalog")
        self.assertEqual(section_manifest.get("filename"), "site_127.0.0.1_catalog.txt")
        self.assertTrue(all("/catalog" in url for url in section_manifest.get("pages", {})),
                        "в манифесте раздела оказались страницы вне раздела")

    def test_section_and_site_files_coexist(self):
        section = self.crawl_section()
        site = self.crawl_site()

        self.assertNotEqual(section["filename"], site["filename"])
        self.assertTrue(self.txt_path(section["filename"]).exists())
        self.assertTrue(self.txt_path(site["filename"]).exists())
        site_text = self.read_txt(site["filename"])
        self.assertIn("Компания основана в 2008 году", site_text)
        self.assertIn("Аэрационные колонны", site_text)

        # оба манифеста на месте и каждый знает свой файл
        section_info = sc.info_for_filename(TEST_USER, section["filename"])
        site_info = sc.info_for_filename(TEST_USER, site["filename"])
        self.assertEqual(section_info["section"], "/catalog")
        # адрес в манифесте — нормализованный (без завершающего слэша, см. normalize_url)
        self.assertEqual(section_info["url"], f"{self.base}/catalog")
        self.assertEqual(site_info["section"], "")
        self.assertEqual(site_info["url"], f"{self.base}/")   # корень сайта слэш сохраняет

    def test_section_recrawl_does_not_take_site_pages(self):
        self.crawl_site()                       # сначала проиндексировали весь сайт
        self.crawl_section()                    # затем раздел
        result = self.crawl_section()           # и повторно раздел

        text = self.read_txt(result["filename"])
        self.assertIn("Аэрационные колонны", text)
        self.assertNotIn("Компания основана в 2008 году", text,
                         "при повторном обходе раздела подтянулись страницы всего сайта")
        self.assertEqual(result["stats"].get("carried", 0), 0)

    def test_section_page_limit_is_honoured(self):
        result = self.crawl_section(limit=1)
        self.assertEqual(result["stats"]["pages"], 1, "обход раздела проигнорировал лимит страниц")
        self.assertEqual(len(sc._load_manifest(TEST_USER, "127.0.0.1", "/catalog").get("pages", {})), 1)

    def test_home_page_with_section_flag_crawls_whole_site(self):
        """Галочка стоит, но введена главная: режим раздела не имеет смысла — обходим сайт."""
        result = self.crawl_section(url=f"{self.base}/")
        self.assertEqual(result["section"], "")
        self.assertEqual(result["filename"], "site_127.0.0.1.txt")
        self.assertIn("Компания основана в 2008 году", self.read_txt(result["filename"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
