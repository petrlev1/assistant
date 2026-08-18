# docs_renderer.py - Мини-рендерер Markdown -> HTML без внешних зависимостей.
"""Лёгкий рендерер Markdown для пользовательской документации (раздел /docs).

Поддерживает подмножество Markdown, достаточное для документации:
- заголовки 1-6 уровней (с якорями для ссылок),
- абзацы, списки (-, *, + и нумерованные),
- таблицы (синтаксис pipe-tables с разделителем |---|),
- цитаты (>), горизонтальная линия (---),
- fenced-блоки кода (```),
- жирный (**), курсив (*), инлайн-код (`), ссылки [текст](url).

Безопасность: весь входной HTML экранируется, рендерится только
разметка из этого списка. Никаких внешних библиотек - работает
одинаково на любой платформе без установки зависимостей.
"""

import html as _html
import re

__all__ = ['render']

# Строка списка: отступ, маркер (-, *, + или "1."), содержимое
_LIST_RE = re.compile(r'^(\s*)([-*+]|\d+\.)\s+(.*)$')
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_HR_RE = re.compile(r'^(-{3,}|\*{3,}|_{3,})$')
# Разделитель таблицы: только | - : и пробелы, минимум один дефис (например |---|---|)
_TABLE_SEP_RE = re.compile(r'^[\s|\-:]+$')
_FENCE = '```'


def _inline(text):
    """Инлайн-разметка одной строки: код, жирный, курсив, ссылки."""
    # Сначала экранируем HTML, затем применяем разметку
    text = _html.escape(text, quote=False)
    # Инлайн-код - раньше остального, чтобы `**` внутри кода остался кодом
    text = re.sub(r'`([^`]+)`', lambda m: '<code>' + m.group(1) + '</code>', text)
    # Жирный **текст** (не пересекается с курсивом)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    # Курсив *текст* (не внутри слова и не двойной)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
    # Ссылки [текст](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', r'<a href="\2">\1</a>', text)
    return text


def _slugify(title):
    """Якорь для заголовка: кириллица/латиница/цифры -> дефисы."""
    slug = re.sub(r'[^a-zа-яё0-9]+', '-', title.lower()).strip('-')
    return slug or 'section'


def _parse_table(lines, i):
    """Таблица из строк | a | b |, начиная со строки i. Возвращает (html, i) или None."""
    header = [c.strip() for c in lines[i].strip().strip('|').split('|')]
    if i + 1 >= len(lines):
        return None
    sep = lines[i + 1].strip()
    if not _TABLE_SEP_RE.match(sep) or '-' not in sep:
        return None
    i += 2
    rows = []
    while i < len(lines):
        line = lines[i].strip()
        if not (line.startswith('|') and line.endswith('|')):
            break
        cells = [c.strip() for c in line.strip('|').split('|')]
        rows.append(cells)
        i += 1

    thead = ''.join(f'<th>{_inline(c)}</th>' for c in header)
    body = ''
    for row in rows:
        cells = ''.join(f'<td>{_inline(c)}</td>' for c in row)
        body += f'<tr>{cells}</tr>'
    return f'<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>', i


def _parse_list(lines, i):
    """Список (ненумерованный или нумерованный), начиная со строки i."""
    items = []
    ordered = None
    while i < len(lines):
        m = _LIST_RE.match(lines[i])
        if not m:
            break
        is_ordered = m.group(2)[0].isdigit()
        if ordered is None:
            ordered = is_ordered
        elif ordered != is_ordered:
            break
        items.append(_inline(m.group(3)))
        i += 1
    tag = 'ol' if ordered else 'ul'
    body = ''.join(f'<li>{it}</li>' for it in items)
    return f'<{tag}>{body}</{tag}>', i


def _parse_quote(lines, i):
    """Цитата (> строки) до первого не-цитатного абзаца."""
    parts = []
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith('>'):
            break
        parts.append(line.lstrip('>').strip())
        i += 1
    return f'<blockquote><p>{_inline(" ".join(parts))}</p></blockquote>', i


def render(md_text):
    """Рендер Markdown-текста в HTML-строку."""
    if md_text is None:
        return ''
    lines = md_text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Fenced-блок кода
        if line.startswith(_FENCE):
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith(_FENCE):
                buf.append(lines[i])
                i += 1
            i += 1  # закрывающий fence
            out.append('<pre><code>' + _html.escape('\n'.join(buf)) + '</code></pre>')
            continue

        # Таблица
        if line.startswith('|'):
            tbl = _parse_table(lines, i)
            if tbl:
                html, i = tbl
                out.append(html)
                continue

        # Список
        if _LIST_RE.match(lines[i]):
            html, i = _parse_list(lines, i)
            out.append(html)
            continue

        # Цитата
        if line.startswith('>'):
            html, i = _parse_quote(lines, i)
            out.append(html)
            continue

        # Заголовок
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            slug = _slugify(title)
            out.append(f'<h{level} id="{slug}">{_inline(title)}</h{level}>')
            i += 1
            continue

        # Горизонтальная линия
        if _HR_RE.match(line):
            out.append('<hr>')
            i += 1
            continue

        # Абзац: склеиваем непустые строки до пустой
        buf = [line]
        i += 1
        while i < n and lines[i].strip():
            buf.append(lines[i].strip())
            i += 1
        out.append(f'<p>{_inline(" ".join(buf))}</p>')

    return '\n'.join(out)


if __name__ == '__main__':
    # Быстрый самопроверочный прогон
    sample = """# Заголовок

Параграф с **жирным**, *курсивом* и `кодом`.

- пункт один
- пункт два

| Колонка A | Колонка B |
|---|---|
| ячейка 1 | ячейка 2 |

> цитата

```
print("hello")
```

[Ссылка](/docs)
"""
    print(render(sample))
