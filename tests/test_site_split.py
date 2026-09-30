# test_site_split.py — дробление больших разделов на подразделы + потолок строк
"""Проверки планировщика дробления (site_crawler.plan_parts_from_urls / plan_split),
работы джоба в режиме дробления и остановки обхода при исчерпании бюджета строк.

Запуск:  venv/Scripts/python.exe tests/test_site_split.py
         (Linux-сервер: venv/bin/python tests/test_site_split.py)
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
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import site_crawler as sc

TEST_USER = 990002
BUDGET = 3      # страниц на файл в тестах


def urls(base, paths):
    return [base + p for p in paths]


class PlanCase(unittest.TestCase):
    """Планировщик: чистая арифметика над списком адресов."""

    def test_groups_by_subsection(self):
        """Раздел делится по подразделам первого уровня, порядок карты сайта сохраняется."""
        base = "https://site.ru"
        plan = sc.plan_parts_from_urls(urls(base, [
            "/catalog/a/1.html", "/catalog/a/2.html", "/catalog/b/1.html", "/catalog/b/2.html",
        ]), "/catalog", BUDGET)
        self.assertEqual([p["prefix"] for p in plan["parts"]],
                         ["/catalog/a", "/catalog/b"])
        self.assertEqual([len(p["urls"]) for p in plan["parts"]], [2, 2])
        self.assertEqual(plan["own"], [])

    def test_recurses_only_into_oversized_subsections(self):
        """Подраздел больше бюджета — уходим на уровень глубже, остальные не трогаем."""
        base = "https://site.ru"
        paths = ([f"/catalog/big/g{i}/1.html" for i in range(4)] +      # 4 страницы в /catalog/big
                 [f"/catalog/small/{i}.html" for i in range(2)])
        plan = sc.plan_parts_from_urls(urls(base, paths), "/catalog", BUDGET)
        prefixes = [p["prefix"] for p in plan["parts"]]
        self.assertEqual(prefixes, ["/catalog/big/g0", "/catalog/big/g1", "/catalog/big/g2",
                                    "/catalog/big/g3", "/catalog/small"])
        self.assertEqual(len(plan["oversized"]), 0)

    def test_root_pages_kept_separate(self):
        """Страницы самого раздела не размазываются по подразделам: они идут первым файлом."""
        base = "https://site.ru"
        plan = sc.plan_parts_from_urls(urls(base, [
            "/catalog/", "/catalog/a/1.html", "/catalog/a/2.html",
        ]), "/catalog", BUDGET)
        self.assertEqual(plan["own"], [base + "/catalog/"])
        self.assertEqual([p["prefix"] for p in plan["parts"]], ["/catalog/a"])

    def test_marks_unsplittable_at_max_depth(self):
        """Ниже предельной глубины не дробим: часть помечается как невлезающая."""
        base = "https://site.ru"
        paths = [f"/catalog/a/b/c/d/f{i}.html" for i in range(5)]     # глубже предела SPLIT_MAX_DEPTH
        plan = sc.plan_parts_from_urls(urls(base, paths), "/catalog", 2, max_depth=3)
        self.assertTrue(plan["oversized"])
        self.assertEqual(len(plan["parts"]), 1)


class PlanSplitCase(unittest.TestCase):
    """plan_split: решение по карте сайта (кэш site_cache/user_N/<домен>/sitemap_urls.json)."""

    DOMAIN = "localhost-section.test"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rag_split_plan_")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        folder = sc._site_dir(TEST_USER, self.DOMAIN)
        os.makedirs(folder, exist_ok=True)
        self.cache_path = os.path.join(folder, "sitemap_urls.json")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_sitemap(self, paths):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"urls": [f"https://{self.DOMAIN}{p}" for p in paths]}, f)

    def test_unknown_without_sitemap_cache(self):
        """Предпросмотр не ходит в сеть: без кэша карты сайта план неизвестен."""
        plan = sc.plan_split(TEST_USER, f"https://{self.DOMAIN}/catalog/", use_cache_only=True)
        self.assertIsNone(plan["needed"])
        self.assertEqual(plan["reason"], "unknown")

    def test_small_section_fits_single_file(self):
        self._write_sitemap(["/catalog/a/1.html", "/catalog/a/2.html"])
        plan = sc.plan_split(TEST_USER, f"https://{self.DOMAIN}/catalog/",
                             page_budget=10, use_cache_only=True)
        self.assertFalse(plan["needed"])
        self.assertEqual(plan["reason"], "fits")

    def test_big_section_is_split_into_subsections(self):
        """Раздел больше бюджета — план из подразделов, корневая страница идёт первому файлу."""
        self._write_sitemap(["/catalog/"] + [f"/catalog/a/{i}.html" for i in range(4)] +
                            [f"/catalog/b/{i}.html" for i in range(4)])
        plan = sc.plan_split(TEST_USER, f"https://{self.DOMAIN}/catalog/",
                             page_budget=3, use_cache_only=True)
        self.assertTrue(plan["needed"])
        self.assertEqual([p["prefix"] for p in plan["parts"]], ["/catalog/a", "/catalog/b"])
        self.assertEqual(plan["parts"][0]["extra_seeds"], [f"https://{self.DOMAIN}/catalog/"])
        self.assertNotIn("extra_seeds", plan["parts"][1])
        self.assertEqual(plan["parts"][0]["url"], f"https://{self.DOMAIN}/catalog/a/")
        self.assertEqual(plan["parts"][0]["filename"],
                         sc.site_filename(self.DOMAIN, "/catalog/a"))

    def test_untouched_by_other_prefixes(self):
        """В план попадают только страницы запрошенного раздела."""
        self._write_sitemap(["/catalog/a/1.html", "/catalog/a/2.html", "/catalog/a/3.html",
                             "/news/1.html", "/news/2.html"])
        plan = sc.plan_split(TEST_USER, f"https://{self.DOMAIN}/catalog/",
                             page_budget=2, use_cache_only=True)
        self.assertEqual(plan["pages"], 3, "чужие разделы попали в план")
        # единственный подраздел = один файл: дробить нечего, обход пойдёт как обычно
        self.assertFalse(plan["needed"])
        self.assertEqual(plan["oversized"], ["/catalog/a"])

    def test_section_without_subsections_reports_unsplittable(self):
        """Дробить не на что (все страницы — файлы раздела): один файл, часть страниц не влезет."""
        self._write_sitemap([f"/catalog/{i}.html" for i in range(5)])
        plan = sc.plan_split(TEST_USER, f"https://{self.DOMAIN}/catalog/",
                             page_budget=2, use_cache_only=True)
        self.assertFalse(plan["needed"])
        self.assertEqual(plan["reason"], "unsplittable")
        self.assertEqual(plan["parts"], [])
        self.assertEqual(plan["oversized"], ["/catalog"])


class CrawlBudgetCase(unittest.TestCase):
    """Обход останавливается, когда строковый бюджет файла исчерпан (не качает впустую)."""

    PAGES = {
        "catalog/index.html": """<!doctype html><html><head><title>Каталог разделов</title></head><body>
            <h1>Каталог водоподготовки</h1>
            <p>В каталоге собраны системы водоочистки, комплектующие и реагенты для водоподготовки дома.</p>
            <a href="/catalog/a/1.html">Раздел A</a><a href="/catalog/a/2.html">Ещё</a></body></html>""",
        "catalog/a/1.html": """<html><head><title>A1</title></head><body>
            <p>Первая страница раздела A содержит описание оборудования для очистки воды в загородном доме.</p>
            </body></html>""",
        "catalog/a/2.html": """<html><head><title>A2</title></head><body>
            <p>Вторая страница раздела A описывает комплектующие и расходные материалы для систем очистки.</p>
            </body></html>""",
    }

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_split_crawl_")
        cls.site_dir = os.path.join(cls.tmp, "site_src")
        os.makedirs(os.path.join(cls.site_dir, "catalog", "a"), exist_ok=True)
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)

        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
            *a, directory=cls.site_dir, **kw)
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        cls.server.allow_reuse_address = True
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        for name, content in cls.PAGES.items():
            path = os.path.join(cls.site_dir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ[sc.ALLOW_LOCAL_ENV] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop(sc.ALLOW_LOCAL_ENV, None)
        cls.server.shutdown()
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_crawl_stops_when_line_budget_is_spent(self):
        """Потолок строк достигнут — обход прекращается, а не качает остальные страницы."""
        real_max = sc.MAX_LINES
        sc.MAX_LINES = 2
        try:
            result = sc.crawl(TEST_USER, self.base + "/catalog/", 10, section=True)
        finally:
            sc.MAX_LINES = real_max

        self.assertTrue(result["stats"]["truncated"], "файл не помечен как обрезанный")
        self.assertLess(result["stats"]["pages"], 3,
                        "обход продолжился после исчерпания бюджета строк")
        self.assertGreaterEqual(result["lines"], 1, "в файле не осталось ни одной строки")

    def test_crawl_collects_all_pages_when_budget_allows(self):
        """При большом бюджете обход собирает все страницы раздела."""
        result = sc.crawl(TEST_USER, self.base + "/catalog/", 10, section=True)
        self.assertFalse(result["stats"]["truncated"])
        self.assertEqual(result["stats"]["pages"], 3)
        self.assertEqual(result["lines"], 3)


class SplitJobCase(unittest.TestCase):
    """Джоб дробления: обходит каждый подраздел отдельно и индексирует всё одним разом."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag_split_job_")
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)
        os.environ[sc.ALLOW_LOCAL_ENV] = "1"
        cls.base = "http://127.0.0.1:9"          # crawl подменён, сеть не нужна

    @classmethod
    def tearDownClass(cls):
        os.environ.pop(sc.ALLOW_LOCAL_ENV, None)
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        deadline = time.time() + 20
        while time.time() < deadline and sc._JOB["active"]:
            time.sleep(0.1)
        with sc._JOB_LOCK:
            sc._JOB.update(active=False, job=None, finished_at=0.0)

    def _run_job(self, plan, results_for):
        """Запускает джоб с подменёнными plan_split и crawl, возвращает (итог, вызовы, индексации)."""
        calls, indexed_parts, indexed_single = [], [], []
        real_plan, real_crawl = sc.plan_split, sc.crawl

        def fake_plan(user_id, start_url, respect_robots=True, page_budget=None, use_cache_only=False):
            return plan

        def fake_crawl(user_id, start_url, page_limit=sc.DEFAULT_PAGE_LIMIT, respect_robots=True,
                       on_progress=None, cancel_event=None, section=False, extra_seeds=None):
            calls.append({"url": start_url, "section": section, "seeds": extra_seeds})
            return results_for(start_url)

        sc.plan_split, sc.crawl = fake_plan, fake_crawl
        try:
            sc.start_job(TEST_USER, self.base + "/catalog/", 100, section=True, split=True,
                         on_finish=lambda uid, res: indexed_single.append(res),
                         on_finish_parts=lambda uid, res: indexed_parts.append(res))
            deadline = time.time() + 20
            while time.time() < deadline and sc._JOB["active"]:
                time.sleep(0.1)
            job = sc.public_job(sc._JOB.get("job") or {})
        finally:
            sc.plan_split, sc.crawl = real_plan, real_crawl
        return job, calls, indexed_parts, indexed_single

    def _result(self, url, filename, lines, pages=2):
        return {"ok": True, "domain": "127.0.0.1", "filename": filename, "url": url,
                "section": sc.section_prefix(sc.normalize_url(url)), "lines": lines,
                "stats": {"pages": pages, "lines": lines, "cancelled": False}}

    def test_each_part_is_crawled_and_indexed_once(self):
        """По обходу на подраздел, документы добавляются пачкой с одной переиндексацией."""
        plan = {"needed": True, "reason": "split", "pages": 6, "page_budget": 3,
                "parts": [{"prefix": "/catalog/a", "url": self.base + "/catalog/a/",
                           "pages": 3, "filename": "site_127.0.0.1_catalog_a.txt",
                           "extra_seeds": [self.base + "/catalog/"]},
                          {"prefix": "/catalog/b", "url": self.base + "/catalog/b/",
                           "pages": 3, "filename": "site_127.0.0.1_catalog_b.txt"}]}

        def results_for(url):
            name = "site_127.0.0.1_catalog_a.txt" if url.endswith("/a/") else "site_127.0.0.1_catalog_b.txt"
            return self._result(url, name, 40)

        job, calls, parts, single = self._run_job(plan, results_for)

        self.assertEqual([c["url"] for c in calls],
                         [self.base + "/catalog/a/", self.base + "/catalog/b/"])
        self.assertEqual(calls[0]["seeds"], [self.base + "/catalog/"])   # корень раздела — первому файлу
        self.assertIsNone(calls[1]["seeds"])
        self.assertTrue(all(c["section"] for c in calls), "подразделы обходятся как разделы")
        self.assertEqual(len(parts), 1, "переиндексация должна быть одна")
        self.assertEqual(len(parts[0]), 2, "в базу знаний должны попасть оба файла")
        self.assertEqual(single, [], "одиночный обработчик вызываться не должен")
        self.assertTrue(job["indexed"])
        self.assertEqual(job["parts_total"], 2)
        self.assertEqual(job["pages"], 4)
        self.assertEqual(job["lines"], 80)
        self.assertIn("2 файлов", job["message"])

    def test_small_section_stays_single_file(self):
        """План сказал «влезает» — обход один, документ один."""
        plan = {"needed": False, "reason": "fits", "pages": 3, "page_budget": 100, "parts": []}
        job, calls, parts, single = self._run_job(
            plan, lambda url: self._result(url, "site_127.0.0.1_catalog.txt", 30))

        self.assertEqual(len(calls), 1)
        self.assertEqual(parts, [])
        self.assertEqual(len(single), 1, "одиночный файл должен идти в обычный обработчик")
        self.assertTrue(job["indexed"])

    def test_cancel_stops_between_parts(self):
        """Отмена между подразделами: обход прекращается, собранное всё равно индексируется."""
        plan = {"needed": True, "reason": "split", "pages": 6, "page_budget": 3,
                "parts": [{"prefix": f"/catalog/p{i}", "url": self.base + f"/catalog/p{i}/",
                           "pages": 3, "filename": f"site_127.0.0.1_catalog_p{i}.txt"}
                          for i in range(3)]}

        real_plan, real_crawl = sc.plan_split, sc.crawl
        calls = []

        def fake_plan(*args, **kwargs):
            return plan

        def fake_crawl(user_id, start_url, page_limit=sc.DEFAULT_PAGE_LIMIT, respect_robots=True,
                       on_progress=None, cancel_event=None, section=False, extra_seeds=None):
            calls.append(start_url)
            if len(calls) == 1:
                cancel_event.set()          # отменяем сразу после первого подраздела
            return self._result(start_url, "site_127.0.0.1_catalog_p0.txt", 10)

        parts = []
        sc.plan_split, sc.crawl = fake_plan, fake_crawl
        try:
            sc.start_job(TEST_USER, self.base + "/catalog/", 100, section=True, split=True,
                         on_finish_parts=lambda uid, res: parts.append(res))
            deadline = time.time() + 20
            while time.time() < deadline and sc._JOB["active"]:
                time.sleep(0.1)
        finally:
            sc.plan_split, sc.crawl = real_plan, real_crawl

        self.assertEqual(len(calls), 1, "после отмены следующие подразделы не обходятся")
        self.assertEqual(len(parts), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
