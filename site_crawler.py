# site_crawler.py — импорт сайта в базу знаний: обход сайта → TXT со строками-фактами
"""Обход сайта по ссылке и сборка текстового файла для базы знаний.

Схема работы:

    URL → [обход страниц] → html + извлечённый текст в site_cache/user_N/<домен>/
                          → строки-факты → Database/user_N/site_<домен>.txt
                          → дальше штатный конвейер (add_document + переиндексация),
                            который сам создаёт pkl эмбеддингов и BM25.

    URL → [обход страниц] → строки-факты → Database/user_N/site_<домен>_<раздел>.txt

Режим раздела (`section=True`, галочка «Только этот раздел»): обходятся не все
страницы сайта, а только те, чей путь лежит под путём введённого адреса, — у раздела
свой файл БЗ, своя запись в «Документах» и свой pkl эмбеддингов. Кэш страниц общий
на домен, поэтому обход раздела не перекачивает страницы, уже взятые общим обходом.

Правила сборки TXT (совпадают с форматом БЗ проекта, см. навык
rag-knowledge-base-formatting): одна строка = один факт, 40–250 символов,
ключевые слова в начале строки, строки-разделители начинаются с «#» (система их
не индексирует, человеку они помогают навигировать по файлу), дубликаты по всему
сайту убираются, короткие безличные куски получают контекст раздела.

Повторный запуск по тому же URL инкрементальный: страницы запрашиваются с
If-None-Match / If-Modified-Since, ответ 304 — текст берётся из кэша, txt
перезаписывается целиком, эмбеддинги пересчитывает существующий конвейер
(сначала по хэшу всего файла: не изменилось — берётся готовый pkl; если файл
изменился — по построчному кэшу векторов, считаются только новые строки).

Зависимостей вне stdlib нет (requests уже в проекте). Если в окружении есть
trafilatura, она используется для выделения основного текста вместо встроенного
разборщика — это необязательно (pip install trafilatura).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

# === Параметры обхода (значения по умолчанию; лимит страниц может приходить из
# настроек приложения app_settings.site_pages_limit или из запроса) ===

DEFAULT_PAGE_LIMIT = 300        # страниц за один обход (раздел каталога в 50 не влезал)
MAX_PAGE_LIMIT = 1000           # потолок для настройки app_settings.site_pages_limit
SECTION_SLUG_MAX_CHARS = 60     # длина слага раздела в имени файла БЗ
MAX_LINES = 10000               # больше строк с одного сайта не берём
# Какую долю потолка строк отдаём под страницы одного файла: остаток — запас на то,
# что страницы внутри раздела плотнее среднего по домену (иначе файл молча обрежется).
SITE_FILE_SAFETY_PERCENT = 90
MAX_LINE_CHARS = 250            # длиннее — режем по предложениям
MIN_LINE_CHARS = 40             # короче — только с контекстом раздела, иначе мусор-навигация
MIN_SECTION_CHARS = 15          # осмысленный заголовок раздела
SPLIT_MAX_DEPTH = 4             # глубже 4 сегментов не дробим: слишком мелкие файлы
DEFAULT_LINES_PER_PAGE = 60     # оценка плотности, пока нет своих замеров по домену
SITEMAP_CACHE_TTL = 6 * 3600    # сколько держать список адресов карты сайта
MAX_PAGE_BYTES = 3 * 1024 * 1024
PAGE_TIMEOUT = 10
CRAWL_DELAY = 0.3               # пауза между запросами, сек
MAX_REDIRECTS = 5
MAX_SITEMAPS = 25               # под-карт сайта бывает много: карту каталога (iblock) читаем тоже
USER_AGENT = "RAGSTONE-bot/1.0 (+https://ragstone.ru)"
CACHE_ROOT = "site_cache"
DATABASE_ROOT = "Database"
JOB_KEEP_SECONDS = 300          # сколько держать итог последнего обхода (F5 не теряет результат)

# Переменная окружения для локальных тестов: разрешает обход localhost/приватных
# адресов (по умолчанию запрещено — защита от SSRF, сайт публичный).
ALLOW_LOCAL_ENV = "SITE_CRAWLER_ALLOW_LOCAL"

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_TRACKING_PARAMS = ("utm_", "yclid", "gclid", "fbclid", "from", "ref", "_openstat", "mc_eid")


class CrawlError(Exception):
    """Ошибка обхода — текст показывается пользователю в интерфейсе."""


# === URL: нормализация и проверка адреса (SSRF) ===

def local_allowed() -> bool:
    """Разрешены ли локальные адреса (только для тестов через env)."""
    return os.environ.get(ALLOW_LOCAL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def normalize_url(url: str) -> str:
    """Приводит URL к каноническому виду: без решётки, без utm-метрик, без лишнего порта.

    Одинаковые страницы с разными метками (utm_source и т.п.) не должны обходиться
    дважды — иначе ждём двойной расход лимита и дубли в тексте.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not any(k.lower().startswith(p) for p in _TRACKING_PARAMS)]
    return urlunparse((scheme, netloc, path, "", urlencode(query), ""))


def normalize_redirect(url: str) -> str:
    """Нормализация адреса из Location: как normalize_url, но завершающий слэш значим.

    normalize_url убирает хвостовой слэш, чтобы /a и /a/ не обходились дважды. Но для
    каталогов слэш — часть канонического адреса: сервер отвечает 301 с /catalog на
    /catalog/ , и без слэша мы бесконечно ходили по кругу, а страница-каталог (как раз
    типичный адрес раздела) выпадала из обхода.
    """
    normalized = normalize_url(url)
    if not normalized:
        return ""
    if (urlparse(url).path or "/").endswith("/"):
        parsed = urlparse(normalized)
        if not parsed.path.endswith("/"):
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path + "/", "", parsed.query, ""))
    return normalized


def same_host(a: str, b: str) -> bool:
    """Один и тот же сайт (www.example.com и example.com считаются одним)."""
    ha = (urlparse(a).hostname or "").lower()
    hb = (urlparse(b).hostname or "").lower()
    if ha.startswith("www."):
        ha = ha[4:]
    if hb.startswith("www."):
        hb = hb[4:]
    return bool(ha) and ha == hb


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def assert_public_url(url: str) -> None:
    """Проверяет, что адрес безопасно запрашивать с сервера (защита от SSRF).

    Публичный сервис: любой зарегистрированный пользователь может ввести URL,
    поэтому http(s)-схема обязательна, а адрес не должен вести на localhost,
    внутреннюю сеть, link-local (169.254.169.254 — метаданные облака) и т.п.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CrawlError("Разрешены только ссылки http:// и https://")
    host = parsed.hostname
    if not host:
        raise CrawlError("В ссылке нет имени сайта")
    if local_allowed():
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise CrawlError(f"Не удалось определить адрес сайта {host}")
    if not infos:
        raise CrawlError(f"Не удалось определить адрес сайта {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise CrawlError("Ссылки на внутренние и служебные адреса запрещены")


# === Кэш страниц и манифест обхода ===

def _site_dir(user_id: int, domain: str) -> str:
    return os.path.join(CACHE_ROOT, f"user_{user_id}", domain)


def section_prefix(url: str) -> str:
    """Префикс пути для режима «только этот раздел» ('' — главная страница сайта).

    /catalog/ → '/catalog'. Сравнение идёт по сегментам пути: /catalog не должен
    захватывать /catalog-sale/ или /catalogovyj.
    """
    path = (urlparse(url).path or "/").rstrip("/")
    return "" if path in ("", "/") else path


def section_slug(prefix: str) -> str:
    """Слаг раздела для имени файла БЗ: '/catalog/termostaty' → 'catalog_termostaty'."""
    slug = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", (prefix or "").strip("/")).strip("_")
    if not slug:
        return ""
    if len(slug) > SECTION_SLUG_MAX_CHARS:
        # Длинный путь в имя файла не влезает: добавляем хвост хэша, иначе два
        # разных раздела склеятся в один файл БЗ.
        digest = hashlib.md5(prefix.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug[:SECTION_SLUG_MAX_CHARS].rstrip('_')}_{digest}"
    return slug


def _manifest_path(user_id: int, domain: str, prefix: str = "") -> str:
    """Манифест обхода — свой на каждый файл БЗ (весь сайт и каждый раздел отдельно).

    Иначе повторный обход раздела затирал бы манифест обхода всего сайта: пропадала
    бы статистика и «Обновить с сайта» у другого файла перестала бы работать.
    """
    slug = section_slug(prefix)
    return os.path.join(_site_dir(user_id, domain),
                        f"manifest_{slug}.json" if slug else "manifest.json")


def _load_manifest(user_id: int, domain: str, prefix: str = "") -> dict:
    try:
        with open(_manifest_path(user_id, domain, prefix), "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("pages", {})
                return data
    except (OSError, ValueError):
        pass
    return {}


def _save_manifest(user_id: int, domain: str, manifest: dict, prefix: str = "") -> None:
    path = _manifest_path(user_id, domain, prefix)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _cache_file(user_id: int, domain: str, kind: str, url: str) -> str:
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    ext = "html" if kind == "html" else "txt"
    return os.path.join(_site_dir(user_id, domain), kind, f"{name}.{ext}")


def site_filename(domain: str, prefix: str = "") -> str:
    """Имя файла БЗ: site_<домен>.txt (весь сайт) или site_<домен>_<раздел>.txt.

    Раздел получает отдельный файл — свою запись в «Документах», свой документ БЗ
    и свой pkl эмбеддингов, поэтому обход раздела не переписывает базу по сайту.
    """
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я._-]", "_", domain or "site")
    slug = section_slug(prefix)
    return f"site_{safe}_{slug}.txt" if slug else f"site_{safe}.txt"


def in_section(url: str, start_url: str, prefix: str = "") -> bool:
    """Страница относится к обходу: тот же сайт и (для раздела) путь под префиксом."""
    if not same_host(url, start_url):
        return False
    if not prefix:
        return True
    path = (urlparse(url).path or "/").rstrip("/") or "/"
    return path == prefix or path.startswith(prefix + "/")


def list_manifests(user_id: int, domain: str) -> list:
    """Все сохранённые обходы домена: весь сайт и каждый раздел отдельным файлом.

    Нужно интерфейсу: показать, что уже проиндексировано по этому сайту, и
    предупредить о пересечении разделов до запуска обхода.
    """
    site_dir = _site_dir(user_id, domain)
    if not os.path.isdir(site_dir):
        return []
    found = []
    for name in sorted(os.listdir(site_dir)):
        if not name.startswith("manifest") or not name.endswith(".json"):
            continue
        prefix = "" if name == "manifest.json" else name[len("manifest_"):-len(".json")]
        manifest = _load_manifest(user_id, domain, prefix)
        if not manifest:
            continue
        found.append({
            "prefix": manifest.get("section", "") or "",
            "filename": manifest.get("filename", ""),
            "url": manifest.get("start_url", ""),
            "pages": len(manifest.get("pages", {})),
            "lines": manifest.get("lines", 0),
            "fetched_at": manifest.get("fetched_at", ""),
        })
    return found


def info_for_filename(user_id: int, filename: str) -> dict | None:
    """Сведения о последнем обходе по имени файла БЗ (для кнопки «Обновить с сайта»).

    У домена может быть несколько файлов (весь сайт и отдельные разделы), поэтому
    просматриваются все манифесты каталога домена. Манифест помнит режим обхода,
    так что обновление раздела не превращается в обход всего сайта.
    """
    root = os.path.join(CACHE_ROOT, f"user_{user_id}")
    if not os.path.isdir(root):
        return None
    for domain in sorted(os.listdir(root)):
        if not os.path.isdir(_site_dir(user_id, domain)):
            continue
        for name in sorted(os.listdir(_site_dir(user_id, domain))):
            if not name.startswith("manifest") or not name.endswith(".json"):
                continue
            prefix = "" if name == "manifest.json" else name[len("manifest_"):-len(".json")]
            manifest = _load_manifest(user_id, domain, prefix)
            if manifest.get("filename") != filename:
                continue
            return {
                "url": manifest.get("start_url", ""),
                "domain": domain,
                "filename": manifest.get("filename", filename),
                "section": manifest.get("section", "") or "",
                "page_limit": manifest.get("page_limit", DEFAULT_PAGE_LIMIT),
                "respect_robots": manifest.get("respect_robots", True),
                "pages": len(manifest.get("pages", {})),
                "lines": manifest.get("lines", 0),
                "fetched_at": manifest.get("fetched_at", ""),
            }
    return None


# === Разбор HTML (stdlib; trafilatura используется, если установлена) ===

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "param", "source", "track", "wbr"}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe",
              "form", "select", "option", "button", "video", "audio", "map", "nav",
              "footer", "header", "aside"}
_BLOCK_TAGS = {"p", "li", "td", "th", "dd", "dt", "blockquote", "figcaption", "pre",
               "address", "summary", "caption", "h1", "h2", "h3", "h4", "h5", "h6",
               "div", "section", "article", "main", "tr", "ul", "ol", "table"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4"}
# Обвязка сайта: меню, подвал, куки-баннеры, соцсети — по именам класса/id
_BOILERPLATE_RE = re.compile(
    r"(menu|nav|navbar|breadcrumb|cookie|footer|header|sidebar|banner|modal|popup|"
    r"social|share|subscribe|newsletter|search|lang|topbar|megamenu|pagination|"
    r"feedback|callback|widget|advert|banner|promo-?popup)", re.I)


def _is_boilerplate(attrs: dict) -> bool:
    marker = " ".join(filter(None, (attrs.get("class", ""), attrs.get("id", ""))))
    return bool(marker) and bool(_BOILERPLATE_RE.search(marker))


class _SiteParser(HTMLParser):
    """Разбор страницы: заголовок, текстовые блоки в порядке следования, ссылки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.blocks = []          # [(тег, текст)]
        self.links = []           # href-ы в порядке появления
        self._skip = []           # стек пропускаемых тегов
        self._open_blocks = []    # стек открытых блочных тегов
        self._buf = []
        self._buf_tag = None
        self._in_title = False

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if text and self._buf_tag:
            self.blocks.append((self._buf_tag, text))
        self._buf = []
        self._buf_tag = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = " ".join((attrs.get("content") or "").split())
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            # Ссылки собираем ВСЕГДА, даже внутри пропускаемых блоков (меню, подвал):
            # они нужны не для текста базы знаний, а для обхода сайта — вёрстка меню
            # часто единственный источник ссылок на остальные страницы.
            href = (attrs.get("href") or "").strip()
            if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                self.links.append(href)
        if tag in _VOID_TAGS:
            if tag == "br" and not self._skip:
                self._flush()
            return
        if self._skip:
            self._skip.append(tag)
            return
        if tag in _SKIP_TAGS or _is_boilerplate(attrs):
            self._flush()
            self._skip.append(tag)
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._open_blocks.append(tag)
            self._buf_tag = tag

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag in _VOID_TAGS:
            return
        if self._skip:
            if tag in self._skip:
                # закрываем всё до этого тега включительно (вёрстка часто «забывает» закрыть)
                while self._skip:
                    if self._skip.pop() == tag:
                        break
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in self._open_blocks:
                while self._open_blocks:
                    if self._open_blocks.pop() == tag:
                        break

    def handle_data(self, data):
        if self._in_title:
            if not self.title:
                self.title = " ".join(data.split())
            return                      # текст <title> в блоки не попадает (иначе дублируется)
        if self._skip:
            return
        if self._buf_tag is None and data.strip():
            self._buf_tag = self._open_blocks[-1] if self._open_blocks else "text"
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _text_only(html: str) -> str:
    """Грубое вытаскивание текста — резерв, если разборщик ничего не дал."""
    return _TAG_RE.sub(" ", html or "")


def extract_page(html: str) -> tuple[str, str, list, list]:
    """HTML → (title, description, blocks, links)."""
    parser = _SiteParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as e:                      # битая вёрстка не должна ронять обход
        logger.warning(f"⚠️ Ошибка разбора HTML: {e}")
    blocks, links = parser.blocks, parser.links
    if not blocks:
        plain = " ".join(_text_only(html).split())
        if plain:
            blocks = [("text", plain)]
    return parser.title, parser.description, blocks, links


def extract_main_text(html: str) -> str:
    """Основной текст страницы. trafilatura, если установлена, иначе встроенный разборщик."""
    try:
        import trafilatura                          # необязательная зависимость
        text = trafilatura.extract(html or "", include_comments=False,
                                   include_tables=True, favor_recall=True)
        if text:
            return text
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"⚠️ trafilatura не справилась ({e}), используется встроенный разбор")
    _title, _description, blocks, _links = extract_page(html)
    return "\n".join(text for _tag, text in blocks)


def html_to_lines(title: str, description: str, blocks: list, seen: set) -> list:
    """Текстовые блоки страницы → строки-факты (с дедупликацией по всему сайту).

    seen — общий на весь обход набор нормализованных строк: шапки, футеры и
    повторяющиеся CTA встречаются на каждой странице и без общей дедупликации
    засоряют индекс.
    """
    lines = []
    section = ""
    if description and len(description) >= MIN_LINE_CHARS:
        lines.append(description)
    for tag, raw in blocks:
        text = " ".join((raw or "").split())
        if not text:
            continue
        if tag in _HEADING_TAGS:
            if len(text) >= MIN_SECTION_CHARS:
                section = text
            if len(text) >= MIN_LINE_CHARS:
                lines.append(text)      # длинный заголовок — сам по себе факт
            continue
        for chunk in split_block(text):
            if len(chunk) < MIN_LINE_CHARS:
                context = section or title
                if context:
                    chunk = f"{context}: {chunk}"
            if len(chunk) >= MIN_LINE_CHARS:
                lines.append(chunk)
    result = []
    for line in lines:
        key = " ".join(line.split()).lower()
        if len(key) < MIN_LINE_CHARS or key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def split_block(text: str, max_chars: int = MAX_LINE_CHARS) -> list:
    """Абзац → куски ≤ max_chars: сначала по предложениям, затем жёстко по словам."""
    pieces = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    chunks, current = [], ""
    for piece in pieces:
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            line = ""
            for word in piece.split():
                if line and len(line) + 1 + len(word) > max_chars:
                    chunks.append(line)
                    line = word
                else:
                    line = f"{line} {word}".strip()
            if line:
                chunks.append(line)
            continue
        if current and len(current) + 1 + len(piece) <= max_chars:
            current = f"{current} {piece}"
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


# === Сеть: загрузка страницы с проверкой адреса на каждом шаге ===

_SKIP_EXT_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|bmp|svg|ico|css|js|json|xml|pdf|zip|rar|7z|tar|gz|"
    r"doc|docx|xls|xlsx|ppt|pptx|mp3|mp4|avi|mov|mkv|wav|ogg|woff2?|ttf|eot|apk|dmg|exe)$", re.I)


def _decode(response: requests.Response, body: bytes) -> str:
    """Байты → текст. Кодировка: charset из заголовка → <meta charset> → utf-8 → cp1251.

    Важно не полагаться на response.encoding: requests для text/* без charset
    подставляет ISO-8859-1, и русский UTF-8 текст превращается в «Ð¢ÐµÑ€Ð¼Ð¾Ñ‡ÐµÑ…Ð»Ñ‹».
    """
    candidates = []
    match = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", response.headers.get("Content-Type", ""), re.I)
    if match:
        candidates.append(match.group(1))
    meta = re.search(rb"charset\s*=\s*[\"']?\s*([\w\-]+)", body[:4096], re.I)
    if meta:
        candidates.append(meta.group(1).decode("ascii", errors="ignore"))
    candidates += ["utf-8", "cp1251"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def fetch(session: requests.Session, url: str, etag: str = "",
          last_modified: str = "", expect_html: bool = True) -> dict:
    """GET одной страницы. Редиректы проходим вручную — проверяя КАЖДЫЙ хоп.

    Автоматические редиректы requests позволили бы сайту увести нас на внутренний
    адрес в обход проверки, поэтому allow_redirects=False и проверка на каждом шаге.
    expect_html=False — для robots.txt и sitemap.xml (там text/plain и application/xml).
    """
    headers = {"User-Agent": USER_AGENT,
               "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
               "Accept-Language": "ru,en;q=0.8"}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified

    current = url
    visited_hops = set()
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_url(current)
        if current in visited_hops:
            return {"url": current, "status": 0, "html": None, "error": "Циклический редирект"}
        visited_hops.add(current)
        try:
            response = session.get(current, headers=headers, timeout=PAGE_TIMEOUT,
                                   allow_redirects=False, stream=True)
        except requests.RequestException as e:
            return {"url": current, "status": 0, "html": None, "error": str(e)}
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return {"url": current, "status": response.status_code, "html": None,
                            "error": "Редирект без адреса"}
                current = normalize_redirect(urljoin(current, location))
                if not current:
                    return {"url": url, "status": 0, "html": None, "error": "Некорректный редирект"}
                continue
            content_type = (response.headers.get("Content-Type") or "").lower()
            result = {"url": current, "status": response.status_code, "html": None,
                      "content_type": content_type,
                      "etag": response.headers.get("ETag", ""),
                      "last_modified": response.headers.get("Last-Modified", "")}
            if response.status_code != 200:
                return result
            if expect_html and content_type and "html" not in content_type:
                result["error"] = f"не HTML ({content_type.split(';')[0]})"
                return result
            body, size = b"", 0
            for chunk in response.iter_content(65536):
                body += chunk
                size += len(chunk)
                if size >= MAX_PAGE_BYTES:
                    logger.warning(f"⚠️ Страница {current} обрезана на {MAX_PAGE_BYTES} байт")
                    break
            result["html"] = _decode(response, body)
            return result
        finally:
            response.close()
    return {"url": url, "status": 0, "html": None, "error": "Слишком много редиректов"}


# === robots.txt и sitemap.xml ===

def _robots_rules(session: requests.Session, base_url: str) -> tuple[list, list, list]:
    """(disallow, allow, sitemaps) для нашего User-Agent. Ошибка сети = ничего не запрещаем."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    disallow, allow, sitemaps = [], [], []
    try:
        result = fetch(session, origin + "/robots.txt", expect_html=False)
        body = result.get("html") or ""
        if result.get("status") != 200 or not body:
            return disallow, allow, sitemaps
        groups, current_agents, in_rules = [], [], False
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if in_rules:
                    groups.append((current_agents, disallow, allow))
                    current_agents, disallow, allow, in_rules = [], [], [], False
                current_agents.append(value.lower())
            elif field in ("disallow", "allow"):
                in_rules = True
                target = disallow if field == "disallow" else allow
                target.append(value)
            elif field == "sitemap" and value:
                sitemaps.append(value)
        if current_agents:
            groups.append((current_agents, disallow, allow))
        for agents, d, a in groups:
            if any(ag and (ag in USER_AGENT.lower()) for ag in agents):
                return d, a, sitemaps
        for agents, d, a in groups:
            if "*" in agents:
                return d, a, sitemaps
    except Exception as e:
        logger.warning(f"⚠️ robots.txt недоступен ({e}) — обход без ограничений роботса")
    return [], [], sitemaps


def _robots_allowed(url: str, disallow: list, allow: list) -> bool:
    path = urlparse(url).path or "/"
    best_disallow = max((len(p) for p in disallow if p and path.startswith(p)), default=-1)
    best_allow = max((len(p) for p in allow if p and path.startswith(p)), default=-1)
    return best_disallow <= best_allow


def _sitemap_entries(session: requests.Session, base_url: str, sitemaps: list) -> tuple:
    """Карта сайта (включая sitemapindex) → ({адрес: lastmod}, сколько карт прочитано).

    lastmod нужен для проверки изменений без обхода страниц: у Bitrix он стоит у
    каждой ссылки и обновляется при правке страницы. Пустая строка — карта lastmod
    не отдаёт.
    """
    parsed = urlparse(base_url)
    # robots.txt часто перечисляет одну и ту же карту в разных формах (http/https, с www):
    # без дедупликации счётчик прочитанных карт врёт вдвое
    candidates = list(dict.fromkeys(s for s in sitemaps if s)) or [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"]
    entries, fetched = {}, 0
    seen = set(candidates)
    queue = deque(candidates[:MAX_SITEMAPS])
    while queue and fetched < MAX_SITEMAPS:
        candidate = queue.popleft()
        fetched += 1
        try:
            assert_public_url(candidate)
        except CrawlError:
            continue
        result = fetch(session, candidate, expect_html=False)
        body = result.get("html") or ""
        if result.get("status") != 200 or not body:
            continue
        # Записи страниц: <url><loc>…</loc><lastmod>…</lastmod></url>
        for match in re.finditer(
                r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>\s*(?:<lastmod>\s*([^<\s]+)\s*</lastmod>)?",
                body, re.I):
            entries.setdefault(match.group(1), match.group(2) or "")
        # Вложенные карты из <sitemapindex> — идём на уровень глубже
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I):
            if loc.lower().endswith(".xml") and loc not in seen:
                if fetched + len(queue) < MAX_SITEMAPS:
                    seen.add(loc)
                    queue.append(loc)
    return entries, fetched


def _sitemap_urls(session: requests.Session, base_url: str, sitemaps: list) -> list:
    """Только адреса из карты сайта (обёртка над _sitemap_entries)."""
    return list(_sitemap_entries(session, base_url, sitemaps)[0])


def url_key(url: str) -> str:
    """Ключ страницы для сверки: без хвостового слэша, хост и схема в нижнем регистре.

    Карта сайта отдаёт адреса с хвостовым слэшем («/catalog/a/»), а в манифесте
    страница записана так, как её вернул сервер после редиректов («/catalog/a»).
    Сравнение «как есть» объявляло бы такие страницы новыми на каждой проверке.
    """
    parts = urlparse((url or "").strip())
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def _load_all_manifests(user_id: int, domain: str) -> list:
    """Все манифесты домена: [(раздел, имя файла, данные)] — по одному на файл БЗ."""
    folder = _site_dir(user_id, domain)
    out = []
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("manifest") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        out.append((data.get("section") or "", data.get("filename") or "", data))
    return out


def _parse_moment(value: str):
    """Время из карты сайта или манифеста; время без зоны считаем местным (как и обход)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return moment


def _page_is_newer(lastmod: str, fetched_at: str) -> bool:
    """Страница менялась после нашей выборки? Непонятное время считаем изменением."""
    stamp = _parse_moment(lastmod)
    if stamp is None:
        return bool((lastmod or "").strip())      # lastmod есть, но не разобран — перепроверим
    was = _parse_moment(fetched_at)
    if was is None:
        return True
    return stamp > was


def check_site_changes(user_id: int, start_url: str, respect_robots: bool = True) -> dict:
    """Что изменилось на сайте — по карте сайта, без обхода страниц.

    Карта читается заново (не из кэша) и сравнивается с манифестами файлов домена:
    страница, которой у нас нет, — новая; страница, у которой lastmod новее момента
    нашей выборки, — изменившаяся; страница, которую мы брали из карты и которой там
    больше нет, — пропавшая. Ничего не меняет: только отчёт, обновление запускает юзер.
    """
    start_url = normalize_url(start_url)
    if not start_url:
        raise CrawlError("Некорректный адрес сайта")
    assert_public_url(start_url)
    domain = domain_of(start_url)

    session = requests.Session()
    sitemaps = []
    if respect_robots:
        _disallow, _allow, sitemaps = _robots_rules(session, start_url)
    entries, sitemaps_read = _sitemap_entries(session, start_url, sitemaps)
    has_lastmod = any((stamp or "").strip() for stamp in entries.values())
    entries_by_key = {url_key(url) for url in entries}

    manifests = _load_all_manifests(user_id, domain)
    known = set()                      # ключи страниц, которые у нас уже есть
    for _section, _filename, data in manifests:
        known.update(url_key(url) for url in (data.get("pages") or {}))

    files, total_new, total_changed, total_gone, total_unknown = [], 0, 0, 0, 0
    for section, filename, data in manifests:
        pages = data.get("pages") or {}
        if not pages:
            continue
        by_key = {url_key(url): url for url in pages}
        scope = [url for url in entries if in_section(url, start_url, section)]
        fresh = [url for url in scope if url_key(url) not in known]
        changed = [url for url in scope
                   if url_key(url) in by_key
                   and _page_is_newer(entries.get(url, ""),
                                      (pages.get(by_key[url_key(url)]) or {}).get("fetched_at", ""))]
        gone = [url for url in pages
                if (pages.get(url) or {}).get("source") == "sitemap" and url_key(url) not in entries_by_key]
        # Карта знает страницу, но lastmod не отдаёт — изменение такой страницы по карте
        # не увидеть; считаем отдельно, чтобы отчёт не выдавал это за «всё в порядке».
        unknown = [url for url in scope
                   if url_key(url) in by_key and not (entries.get(url) or "").strip()]
        total_new += len(fresh)
        total_changed += len(changed)
        total_gone += len(gone)
        total_unknown += len(unknown)
        files.append({
            "filename": filename, "section": section, "pages": len(pages),
            "new": len(fresh), "changed": len(changed), "gone": len(gone),
            "unknown": len(unknown),
            "sample_new": [urlparse(u).path for u in fresh[:5]],
            "sample_changed": [urlparse(u).path for u in changed[:5]],
            "sample_gone": [urlparse(u).path for u in gone[:5]],
        })

    affected = [f for f in files if f["new"] or f["changed"] or f["gone"]]
    # Карта без lastmod (или её отсутствие) — сравнивать нечем: честно говорим об этом,
    # иначе «изменений нет» означало бы «мы их не видим».
    if not entries:
        reason, supported = "no_sitemap", False
    elif not has_lastmod:
        reason, supported = "no_lastmod", False
    else:
        reason, supported = "", True
    return {
        "ok": True,
        "domain": domain,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "supported": supported,
        "reason": reason,
        "unknown": total_unknown,
        "sitemaps": sitemaps_read,
        "sitemap_urls": len(entries),
        "known_urls": len(known),
        "files_total": len(files),
        "files_affected": len(affected),
        "new": total_new,
        "changed": total_changed,
        "gone": total_gone,
        "files": affected if affected else files,
    }


# === Обход сайта ===

def crawl(user_id: int, start_url: str, page_limit: int = DEFAULT_PAGE_LIMIT,
          respect_robots: bool = True, on_progress=None, cancel_event=None,
          section: bool = False, extra_seeds=None) -> dict:
    """Обход сайта и запись TXT для базы знаний. Возвращает словарь-итог.

    section=True — обходим только страницы под путём введённого адреса (раздел),
    они попадают в отдельный файл БЗ. Кэш страниц при этом общий на домен.

    extra_seeds — дополнительные адреса вне раздела, которые тоже нужно взять
    (так при дроблении в первый файл попадает корневая страница раздела).

    Инкрементальность: страницы из прошлого обхода запрашиваются с If-None-Match /
    If-Modified-Since. Ответ 304 — текст и ссылки берутся из кэша, страница
    считается неизменённой. Страница, которой больше нет (404/410/403), из файла
    уходит; страницы, до которых обход не добрался (лимит, отмена, сбой сети),
    остаются с прежним текстом — обрыв не должен выкидывать содержимое из БЗ.
    """
    start_url = normalize_url(start_url)
    if not start_url:
        raise CrawlError("Некорректный адрес сайта")
    assert_public_url(start_url)

    page_limit = max(1, min(int(page_limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))
    domain = domain_of(start_url)
    prefix = section_prefix(start_url) if section else ""
    if section and not prefix:
        logger.warning("🌐 Включён режим раздела, но адрес — главная страница: обходим весь сайт")
    filename = site_filename(domain, prefix)
    manifest = _load_manifest(user_id, domain, prefix)
    old_pages = manifest.get("pages", {}) or {}

    session = requests.Session()
    disallow = allow = []
    sitemaps = []
    if respect_robots:
        disallow, allow, sitemaps = _robots_rules(session, start_url)

    def _allowed(url: str) -> bool:
        return (not respect_robots) or _robots_allowed(url, disallow, allow)

    def _budget_spent() -> bool:
        """Строковый бюджет файла исчерпан: дальше страницы в него всё равно не влезут."""
        if stats["lines"] >= MAX_LINES:
            stats["truncated"] = True
            logger.info(f"📏 Файл достиг потолка {MAX_LINES} строк — обход остановлен "
                        f"(страниц: {stats['pages']}, файл {filename})")
            return True
        return False

    def _in_scope(url: str) -> bool:
        """Страница входит в обход: тот же сайт и (для раздела) путь под префиксом."""
        return in_section(url, start_url, prefix)

    stats = {"pages": 0, "changed": 0, "new": 0, "gone": 0, "unchanged": 0, "carried": 0,
             "skipped": 0, "errors": 0, "lines": 0, "truncated": False}
    pages_out = {}       # url -> {"title", "lines", "fetched_at"}
    order = []           # порядок страниц в файле (как обошли)
    seen_keys = set()    # дедупликация строк по всему сайту
    page_sources = {}    # url -> "sitemap" | "link" (откуда узнали о странице)
    queue = deque()

    visited = set()
    hard_gone = set()    # страницы, которых больше нет (404/410) или доступ закрыт роботсом

    def _push(url: str, source: str = ""):
        if len(queue) + len(order) >= page_limit * 4:
            return
        if source:
            page_sources.setdefault(url, source)
        queue.append(url)

    def _enqueue_links(links, base_url: str):
        for href in links:
            try:
                absolute = normalize_url(urljoin(base_url, href))
            except ValueError:
                continue
            if absolute and _in_scope(absolute) and absolute not in visited:
                _push(absolute, "link")

    seeds = [normalize_url(u) for u in (extra_seeds or [])]
    seeds = [u for u in seeds if u]
    _push(start_url)
    for seed in seeds:
        if seed not in visited:
            _push(seed)
    for loc in _sitemap_urls(session, start_url, sitemaps):
        if _in_scope(loc):
            _push(loc, "sitemap")
    # Страницы прошлого обхода обязательно проверяем снова: перелинковка на сайте
    # могла измениться, и страница не должна выпасть из файла только потому, что
    # её перестали линковать (иначе второй обход выкинул бы половину базы).
    for old_url in old_pages:
        if _in_scope(old_url):
            _push(old_url)

    def _progress(phase: str, current: str = ""):
        if on_progress:
            on_progress({"phase": phase, "pages": stats["pages"], "pages_total": page_limit,
                         "changed": stats["changed"], "new": stats["new"], "gone": stats["gone"],
                         "lines": stats["lines"], "current": current, "truncated": stats["truncated"]})

    _progress("crawl", start_url)

    while queue and stats["pages"] < page_limit:
        if cancel_event is not None and cancel_event.is_set():
            stats["cancelled"] = True
            break
        url = normalize_url(queue.popleft())
        if not url or url in visited:
            continue
        visited.add(url)
        if (url not in seeds) and (not _in_scope(url) or _SKIP_EXT_RE.search(urlparse(url).path or "")):
            stats["skipped"] += 1
            continue
        if not _allowed(url):
            stats["skipped"] += 1
            logger.info(f"🤖 robots.txt: пропущена {url}")
            continue

        cached = old_pages.get(url) or {}
        result = fetch(session, url, cached.get("etag", ""), cached.get("last_modified", ""))
        status = result.get("status", 0)
        final_url = result.get("url") or url
        stats["pages"] += 1

        if status == 304 and cached:
            text_path = _cache_file(user_id, domain, "text", url)
            lines = []
            if os.path.exists(text_path):
                with open(text_path, "r", encoding="utf-8") as f:
                    lines = [line.rstrip("\n") for line in f if line.strip()]
            if lines:
                pages_out[url] = {"title": cached.get("title", ""), "lines": lines,
                                  "fetched_at": cached.get("fetched_at", ""),
                                  "etag": cached.get("etag", ""),
                                  "last_modified": cached.get("last_modified", ""),
                                  "source": (old_pages.get(url) or {}).get("source") or page_sources.get(url, "")}
                order.append(url)
                stats["unchanged"] += 1
                stats["lines"] += len(lines) - 1 if lines else 0
                if _budget_spent():
                    _progress("crawl", url)
                    break
                # Ссылки берём из сохранённого HTML: страница не изменилась, но обход
                # должен идти дальше — иначе новые страницы сайта не будут найдены.
                cached_html_path = _cache_file(user_id, domain, "html", url)
                if os.path.exists(cached_html_path):
                    try:
                        with open(cached_html_path, "r", encoding="utf-8") as f:
                            _title, _desc, _blocks, cached_links = extract_page(f.read())
                        _enqueue_links(cached_links, final_url)
                    except OSError as e:
                        logger.warning(f"⚠️ Не удалось прочитать кэш страницы {url}: {e}")
            _progress("crawl", url)
            continue

        if status != 200 or not result.get("html"):
            stats["errors"] += 1
            if status in (403, 404, 410):
                hard_gone.add(url)      # страницы больше нет либо доступ закрыт — уходит из файла
            logger.info(f"⚠️ Страница {url}: статус {status} {result.get('error', '')}".strip())
            _progress("crawl", url)
            continue

        html = result["html"]
        title, description, blocks, links = extract_page(html)
        page_lines = html_to_lines(title, description, blocks, seen_keys)
        html_path = _cache_file(user_id, domain, "html", url)
        text_path = _cache_file(user_id, domain, "text", url)
        os.makedirs(os.path.dirname(html_path), exist_ok=True)
        os.makedirs(os.path.dirname(text_path), exist_ok=True)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        separator = f"# {title or domain} — {final_url}"
        with open(text_path, "w", encoding="utf-8") as f:
            f.write("\n".join([separator] + page_lines) + "\n")
        pages_out[url] = {"title": title, "lines": [separator] + page_lines,
                          "fetched_at": datetime.now().isoformat(timespec="seconds"),
                          "etag": result.get("etag", ""), "last_modified": result.get("last_modified", ""),
                          "source": page_sources.get(url, "")}
        order.append(url)
        stats["new" if url not in old_pages else "changed"] += 1
        stats["lines"] += len(page_lines)
        if _budget_spent():
            _progress("crawl", url)
            break

        _enqueue_links(links, final_url)

        _progress("crawl", url)
        time.sleep(CRAWL_DELAY)

    # Страницы прошлого обхода, которых больше нет (404/410/403), уходят из файла.
    # Все остальные, до которых не добрались (лимит страниц, отмена, сбой сети),
    # остаются — текст для них берётся из кэша: обрыв обхода не должен выкидывать
    # содержимое из базы знаний.
    stats["gone"] = sum(1 for url in old_pages if url in hard_gone)
    carried = []
    for old_url in old_pages:
        if old_url in pages_out or old_url in hard_gone or not _in_scope(old_url):
            continue
        text_path = _cache_file(user_id, domain, "text", old_url)
        if not os.path.exists(text_path):
            continue
        try:
            with open(text_path, "r", encoding="utf-8") as f:
                old_lines = [line.rstrip("\n") for line in f if line.strip()]
        except OSError:
            continue
        if not old_lines:
            continue
        previous = old_pages[old_url] or {}
        pages_out[old_url] = {"title": previous.get("title", ""), "lines": old_lines,
                              "fetched_at": previous.get("fetched_at", ""),
                              "etag": previous.get("etag", ""),
                              "last_modified": previous.get("last_modified", "")}
        order.append(old_url)
        carried.append(old_url)
    stats["carried"] = len(carried)

    if cancel_event is not None and cancel_event.is_set():
        stats["cancelled"] = True

    # === Сборка TXT ===
    _progress("build", "")
    lines_out = []
    for url in order:
        entry = pages_out.get(url) or {}
        page_lines = entry.get("lines") or []
        if not page_lines:
            continue
        lines_out.extend(page_lines)
        if len(lines_out) >= MAX_LINES:
            stats["truncated"] = True
            break
    if len(lines_out) > MAX_LINES:
        lines_out = lines_out[:MAX_LINES]

    txt_path = os.path.join(DATABASE_ROOT, f"user_{user_id}", filename)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    manifest = {
        "start_url": start_url,
        "domain": domain,
        "filename": filename,
        "section": prefix,
        "mode": "section" if prefix else "site",
        "page_limit": page_limit,
        "respect_robots": respect_robots,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "lines": sum(1 for line in lines_out if not line.startswith("#")),
        # Обрыв потолком строк: по такому обходу плотность строк занижена (часть
        # страниц в файл не попала), и в оценке бюджета он годится только как запасной.
        "truncated": bool(stats.get("truncated")),
        "pages": {url: pages_out[url] for url in order},
    }
    _save_manifest(user_id, domain, manifest, prefix)

    return {"ok": True, "domain": domain, "filename": filename, "txt_path": os.path.abspath(txt_path),
            "lines": manifest["lines"], "total_lines": len(lines_out), "stats": stats,
            "url": start_url, "section": prefix, "page_limit": page_limit,
            "respect_robots": respect_robots}


# === Дробление раздела на подразделы ===
# Раздел каталога может быть больше потолка строк на файл. Тогда его режем не
# «по живому» (файл 1, файл 2, ...), а по подразделам: части совпадают с реальными
# разделами каталога, каждая со своим txt, группой и эмбеддингами. Если подраздел
# сам не влезает — уходим на уровень глубже.


def _dir_url(start_url: str, prefix: str) -> str:
    """Адрес каталога по префиксу пути: префикс '/catalog' → '<схема>://<хост>/catalog/'."""
    parts = urlparse(start_url)
    return f"{parts.scheme}://{parts.netloc}{prefix}/"


def _depth(prefix: str) -> int:
    return len([seg for seg in (prefix or "").split("/") if seg])


def lines_per_page_estimate(user_id: int, domain: str) -> int:
    """Средняя плотность строк на страницу по прошлым обходам домена.

    От неё зависит, сколько страниц разумно положить в один файл: потолок задан
    в строках, а карта сайта — в страницах.
    """
    folder = _site_dir(user_id, domain)
    total_lines = total_pages = 0
    cut_lines = cut_pages = 0
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("manifest") and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            pages = data.get("pages") or {}
            lines = int(data.get("lines") or 0)
            if not (pages and lines):
                continue
            if data.get("truncated"):
                # Обход оборван потолком строк: «строк на страницу» здесь — оценка
                # снизу (часть страниц в файл не попала). В расчёт такой манифест
                # идёт только если других данных по домену нет.
                cut_lines += lines
                cut_pages += len(pages)
                continue
            total_lines += lines
            total_pages += len(pages)
    if not (total_pages and total_lines) and cut_pages and cut_lines:
        total_lines, total_pages = cut_lines, cut_pages
    if total_pages and total_lines:
        # +1 — строка-заголовок '#' с адресом страницы: она тоже занимает место в файле.
        return max(1, total_lines // total_pages + 1)
    return DEFAULT_LINES_PER_PAGE


def part_page_budget(user_id: int, domain: str) -> int:
    """Сколько страниц класть в один файл, чтобы он не упёрся в потолок строк.

    Берём не весь потолок, а SITE_FILE_SAFETY_PERCENT от него: страницы внутри
    раздела бывают плотнее среднего по домену, и без запаса файл молча обрезался бы.
    """
    return max(1, (MAX_LINES * SITE_FILE_SAFETY_PERCENT // 100)
               // lines_per_page_estimate(user_id, domain))


def _sitemap_cache_path(user_id: int, domain: str) -> str:
    return os.path.join(_site_dir(user_id, domain), "sitemap_urls.json")


def load_sitemap_urls(user_id: int, domain: str, max_age: int = SITEMAP_CACHE_TTL):
    """Список адресов из карты сайта домена — из кэша, если он свежий (иначе None)."""
    path = _sitemap_cache_path(user_id, domain)
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return None
    if max_age is not None and age > max_age:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            urls = (json.load(f) or {}).get("urls") or []
    except (OSError, ValueError):
        return None
    return urls or None


def _fetch_sitemap_urls(user_id: int, domain: str, session, start_url: str, sitemaps) -> list:
    """Читает карту сайта (включая вложенные) и кэширует список адресов."""
    urls = []
    for loc in _sitemap_urls(session, start_url, sitemaps):
        if loc not in urls:
            urls.append(loc)
    if urls:
        try:
            os.makedirs(_site_dir(user_id, domain), exist_ok=True)
            with open(_sitemap_cache_path(user_id, domain), "w", encoding="utf-8") as f:
                json.dump({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                           "urls": urls}, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить список адресов карты сайта: {e}")
    return urls


def plan_parts_from_urls(urls, prefix: str, page_budget: int, max_depth: int = SPLIT_MAX_DEPTH):
    """Делит адреса раздела на части по подразделам.

    Возвращает {"parts": [{"prefix", "urls", "extra"}], "own": [адреса самого раздела],
    "oversized": [части, которые не влезли в бюджет и не делятся дальше]}.
    В "extra" части лежат страницы самого раздела (например сама /catalog/): их надо
    взять первым файлом, иначе они потеряются. Части идут в порядке карты сайта — так
    файлы не перетасовываются между обходами.
    """
    groups, index, own = [], {}, []
    for url in urls:
        path = urlparse(url).path or "/"
        rest = path[len(prefix):].lstrip("/") if prefix else path.lstrip("/")
        if "/" not in rest:
            own.append(url)          # страница самого раздела либо файл прямо в нём
            continue
        seg = rest.split("/")[0]
        child = f"{prefix}/{seg}"
        if child not in index:
            index[child] = []
            groups.append((child, index[child]))
        index[child].append(url)

    parts, oversized = [], []
    for child, items in groups:
        if len(items) <= page_budget:
            parts.append({"prefix": child, "urls": items, "extra": []})
            continue
        if _depth(child) >= max_depth:
            parts.append({"prefix": child, "urls": items, "extra": []})
            oversized.append(child)
            continue
        deeper = plan_parts_from_urls(items, child, page_budget, max_depth)
        if deeper["parts"]:
            # корневая страница подраздела достаётся первому файлу этого подраздела
            deeper["parts"][0]["extra"].extend(deeper["own"])
            parts.extend(deeper["parts"])
            oversized.extend(deeper["oversized"])
        else:
            # делить дальше нечего: в подразделе только страницы-файлы — берём как есть
            parts.append({"prefix": child, "urls": items, "extra": []})
            oversized.append(child)
    if parts and own:
        parts[0]["extra"].extend(own)            # страницы раздела — первому файлу
    return {"parts": parts, "own": own, "oversized": oversized}


def plan_split(user_id: int, start_url: str, respect_robots: bool = True,
               page_budget: int = None, use_cache_only: bool = False) -> dict:
    """Нужно ли дробить раздел и на какие подразделы.

    use_cache_only=True — ничего не запрашивать у сайта (для предпросмотра в диалоге:
    он вызывается на каждый ввод символа).
    """
    start_url = normalize_url(start_url)
    if not start_url:
        raise CrawlError("Некорректный адрес сайта")
    domain = domain_of(start_url)
    prefix = section_prefix(start_url)
    # Потолок файла задан в строках, поэтому бюджет из плотности строк — жёсткий:
    # переданный лимит страниц может его только уменьшить. Иначе при лимите 1000
    # в часть попало бы больше страниц, чем влезает в MAX_LINES, и текст снова
    # обрезался бы — ровно то, от чего дробление и лечит.
    line_budget = part_page_budget(user_id, domain)
    budget = max(1, min(int(page_budget or line_budget), line_budget))
    urls = load_sitemap_urls(user_id, domain)
    if urls is None and not use_cache_only:
        session = requests.Session()
        sitemaps = []
        if respect_robots:
            _disallow, _allow, sitemaps = _robots_rules(session, start_url)
        urls = _fetch_sitemap_urls(user_id, domain, session, start_url, sitemaps)
    if urls is None:
        return {"needed": None, "reason": "unknown", "page_budget": budget, "pages": 0, "parts": []}

    in_scope = [u for u in urls if in_section(u, start_url, prefix)] if prefix else list(urls)
    base = {"page_budget": budget, "pages": len(in_scope)}
    if not in_scope:
        return dict(base, needed=False, reason="no_sitemap", parts=[])
    if len(in_scope) <= budget:
        return dict(base, needed=False, reason="fits", parts=[])

    plan = plan_parts_from_urls(in_scope, prefix, budget)
    parts = []
    for part in plan["parts"]:
        item = {"prefix": part["prefix"], "url": _dir_url(start_url, part["prefix"]),
                "pages": len(part["urls"]), "filename": site_filename(domain, part["prefix"])}
        if part.get("extra"):
            # страницы самого раздела (например сама /catalog/) берём вместе с первым файлом
            item["extra_seeds"] = list(part["extra"])
        parts.append(item)
    if len(parts) <= 1:
        # дробить не на что: раздел обойдётся одним файлом, часть страниц не влезет
        return dict(base, needed=False, reason="unsplittable", parts=[],
                    oversized=list(plan["oversized"]) or [prefix])
    return dict(base, needed=True, reason="split", parts=parts, oversized=plan["oversized"])


# === Фоновый джоб (одна операция за раз, прогресс и отмена) ===

_JOB_LOCK = threading.Lock()
_JOB = {"active": False, "job": None, "finished_at": 0.0}


def _snapshot() -> dict:
    return dict(_JOB.get("job") or {})


def get_status() -> dict:
    """Состояние текущего обхода: active + снимок прогресса (как /api/delete/status)."""
    with _JOB_LOCK:
        if not _JOB["active"] and _JOB["finished_at"] and \
                time.time() - _JOB["finished_at"] > JOB_KEEP_SECONDS:
            _JOB["job"] = None
        return {"active": bool(_JOB["active"]), "job": _snapshot()}


def cancel() -> bool:
    """Просит текущий обход остановиться (недообойдённое в файл не попадает)."""
    with _JOB_LOCK:
        job = _JOB.get("job")
        if not _JOB["active"] or not job:
            return False
        event = job.get("_cancel_event")
    if event is not None:
        event.set()
        return True
    return False


def start_job(user_id: int, start_url: str, page_limit: int = DEFAULT_PAGE_LIMIT,
              respect_robots: bool = True, section: bool = False, on_finish=None,
              split: bool = False, on_finish_parts=None) -> dict:
    """Запускает обход в фоне. Бросает CrawlError, если обход идёт или адрес небезопасен.

    split=True — если раздел не влезает в потолок строк на файл, он автоматически
    режется по подразделам: по txt на подраздел, и все они сразу попадают в БЗ.
    on_finish(user_id, result) — одиночный файл, on_finish_parts(user_id, results) — части.
    """
    start_url = normalize_url(start_url)
    if not start_url:
        raise CrawlError("Некорректный адрес сайта")
    assert_public_url(start_url)                 # проверяем сразу — ошибку видно в интерфейсе
    page_limit = max(1, min(int(page_limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))
    domain = domain_of(start_url)
    prefix = section_prefix(start_url) if section else ""

    with _JOB_LOCK:
        if _JOB["active"]:
            current = _snapshot()
            raise CrawlError(f"Уже идёт обход сайта {current.get('url', '') or '—'}")
        cancel_event = threading.Event()
        job = {
            "user_id": user_id, "url": start_url, "domain": domain,
            "section": prefix,
            "filename": site_filename(domain, prefix),
            "phase": "crawl", "pages": 0, "pages_total": page_limit, "changed": 0,
            "new": 0, "gone": 0, "lines": 0, "current": "", "truncated": False,
            "started_at": time.time(), "finished_at": 0.0, "message": "",
            "result": None, "split": False, "parts_total": 0, "part_no": 0,
            "part_name": "", "part_lines": 0, "parts": [], "_cancel_event": cancel_event,
        }
        _JOB.update(active=True, job=job, finished_at=0.0)

    progress_keys = ("phase", "pages", "pages_total", "changed", "new", "gone",
                     "lines", "current", "truncated")

    def worker():
        results = []
        try:
            parts = None
            if split and prefix:
                plan = plan_split(user_id, start_url, respect_robots, page_budget=page_limit)
                if plan.get("needed"):
                    parts = plan["parts"]
                    with _JOB_LOCK:
                        job["split"] = True
                        job["parts_total"] = len(parts)
                        job["pages_total"] = plan.get("pages") or page_limit
                        job["message"] = f"Раздел разбит на {len(parts)} подразделов"
                    logger.info(f"✂️ {prefix}: страниц по карте сайта {plan.get('pages')}, "
                                f"файлов {len(parts)} (потолок {MAX_LINES} строк, "
                                f"бюджет {plan.get('page_budget')} страниц на файл)")
                elif plan.get("oversized"):
                    logger.warning(f"✂️ {prefix}: подраздел {', '.join(plan['oversized'])} "
                                   f"не влезает в потолок даже целиком — файл будет обрезан")

            if parts:
                base_pages = base_lines = 0
                for number, part in enumerate(parts, start=1):
                    if cancel_event.is_set():
                        break

                    def on_progress(state, number=number, part=part,
                                    base_pages=base_pages, base_lines=base_lines):
                        with _JOB_LOCK:
                            for key in progress_keys:
                                if key in state:
                                    job[key] = state[key]
                            job["pages"] = base_pages + state.get("pages", 0)
                            job["lines"] = base_lines + state.get("lines", 0)
                            job["part_no"] = number
                            job["part_lines"] = state.get("lines", 0)
                            job["part_name"] = state.get("current") or part["url"]
                            job["filename"] = part["filename"]

                    result = crawl(user_id, part["url"], page_limit, respect_robots,
                                   on_progress=on_progress, cancel_event=cancel_event,
                                   section=True, extra_seeds=part.get("extra_seeds"))
                    results.append(result)
                    base_pages += result["stats"].get("pages", 0)
                    base_lines += result["stats"].get("lines", 0)
                    with _JOB_LOCK:
                        job["parts"].append({"filename": result["filename"],
                                             "section": result.get("section", ""),
                                             "url": part["url"],
                                             "lines": result.get("lines", 0),
                                             "pages": result["stats"].get("pages", 0)})
                        job["pages"] = base_pages
                        job["lines"] = base_lines
            else:
                def on_progress(state):
                    with _JOB_LOCK:
                        for key in progress_keys:
                            if key in state:
                                job[key] = state[key]

                results.append(crawl(user_id, start_url, page_limit, respect_robots,
                                     on_progress=on_progress, cancel_event=cancel_event,
                                     section=section))

            cancelled = any(r["stats"].get("cancelled") for r in results)
            if len(results) > 1:
                job["result"] = {"split": True, "files": [r["filename"] for r in results],
                                 "lines": sum(r.get("lines", 0) for r in results),
                                 "pages": sum(r["stats"].get("pages", 0) for r in results)}
            elif results:
                job["result"] = results[0]
            job["phase"] = "cancelled" if cancelled else "done"
            if cancelled:
                job["message"] = "Обход отменён — в файл попало то, что успели обойти"

            # Файлы записываются и при отменённом обходе, поэтому базу знаний обновляем в
            # обоих случаях: иначе в индексе остаётся прежняя версия файла, а на диске
            # уже другая, и ассистент отвечает по устаревшим данным.
            # В режиме дробления итог уходит обработчику частей, даже если успел
            # собраться только один подраздел (отмена в середине обхода).
            if on_finish_parts and job.get("split") and results:
                try:
                    on_finish_parts(user_id, results)
                    job["indexed"] = True
                    if cancelled:
                        job["message"] = ("Обход отменён — в базу знаний попало то, "
                                          "что успели обойти")
                    else:
                        job["message"] = (f"Раздел разбит на {len(results)} файлов — "
                                          f"все добавлены в базу знаний")
                        job["phase"] = "index"
                except Exception as e:
                    logger.error(f"❌ Не удалось добавить файлы сайта в БЗ: {e}")
                    job["message"] = f"Сайт обойдён, но файлы не добавлены в БЗ: {e}"
            elif on_finish and results:
                result = results[-1]
                try:
                    on_finish(user_id, result)
                    job["indexed"] = True
                    if cancelled:
                        job["message"] = ("Обход отменён — в файл и базу знаний попало то, "
                                          "что успели обойти")
                    else:
                        job["message"] = "Файл добавлен в базу знаний"
                        job["phase"] = "index"
                except Exception as e:
                    logger.error(f"❌ Не удалось добавить файл сайта в БЗ: {e}")
                    job["message"] = f"Сайт обойдён, но файл не добавлен в БЗ: {e}"
        except CrawlError as e:
            job["phase"] = "error"
            job["message"] = str(e)
        except Exception as e:
            logger.exception("❌ Ошибка обхода сайта")
            job["phase"] = "error"
            job["message"] = f"Ошибка обхода: {e}"
        finally:
            with _JOB_LOCK:
                job["finished_at"] = time.time()
                _JOB["active"] = False
                _JOB["finished_at"] = job["finished_at"]

    threading.Thread(target=worker, name=f"site-crawl-{domain_of(start_url)}", daemon=True).start()
    with _JOB_LOCK:
        return _snapshot()


def public_job(job: dict) -> dict:
    """Снимок без внутренних полей (для отдачи в JSON)."""
    return {k: v for k, v in (job or {}).items() if not k.startswith("_")}


def status_json() -> dict:
    state = get_status()
    return {"active": state["active"], "job": public_job(state.get("job"))}


if __name__ == "__main__":                       # ручная проверка: python site_crawler.py <url> [user_id] [pages]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print("Использование: python site_crawler.py <url> [user_id] [pages] [section]")
        raise SystemExit(2)
    _uid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    _limit = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_PAGE_LIMIT
    _section = len(sys.argv) > 4 and sys.argv[4].lower() in ("1", "yes", "true", "section", "раздел")
    _out = crawl(_uid, sys.argv[1], _limit, section=_section,
                 on_progress=lambda s: print(f"  {s['phase']}: {s['pages']}/{s['pages_total']} "
                                             f"строк {s['lines']} {s['current'][:70]}"))
    print(json.dumps({k: v for k, v in _out.items() if k != "stats"}, ensure_ascii=False, indent=1))
    print("stats:", _out["stats"])
