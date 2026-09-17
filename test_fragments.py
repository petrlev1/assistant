# test_fragments.py - отбор фрагментов из PDF/DOCX: порог, колонтитулы, триггер OCR
#
# Что проверяется:
#   1) _page_fragments: порог MIN_FRAGMENT_CHARS (заголовки разделов сохраняются,
#      «Вход Выход»/числа отбрасываются), мусор отсекается фильтром качества,
#      многострочные блоки склеиваются, порядок сохраняется;
#   2) ключевая развязка: страница с одним лишь 50-символьным колонтитулом даёт
#      long_chars == 0 → OCR по-прежнему запускается (текст с чертежей не теряется),
#      а фрагмент-колонтитул при этом попадает в базу;
#   3) _dedupe_key: короткие тексты нормализуются, длинные не дедуплицируются;
#   4) DOCX через _process_file: тот же порог и дедуп повторов-ячеек;
#   5) (если есть PyMuPDF и файл из базы) интеграционная проверка на реальном PDF:
#      фрагментов стало больше, колонтитул ровно один, коротких «огрызков» нет,
#      текст с чертежа (из OCR-кэша) на месте.
#
# Запуск из папки проекта:  venv/Scripts/python.exe test_fragments.py
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import rag_core
from rag_core import MIN_FRAGMENT_CHARS, OCR_TRIGGER_MIN_CHARS, DEDUPE_SHORT_MAX_CHARS

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  -> {extra!r}' if not cond and extra != '' else ''))


def _rag():
    """RAGCore без конструктора: нужны только чистые методы отбора."""
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.current_user_id = None
    return rag


LONG = 'Напорная аэрация с компрессором удаляет железо и марганец из воды, окисляя их кислородом воздуха.'
HEADING = 'Конструкция аэрационной колонны'
HEADER50 = 'Аэрационная колонна | Паспорт технического изделия'
TINY = 'Вход Выход'
JUNK = '/uni0422/uni0415/uni0420/uni041C/uni041E/uni0427/uni0415 /uni041F/uni041E'


def test_page_fragments():
    print('\n1) _page_fragments: порог и фильтры')
    print(f'   пороги: MIN_FRAGMENT_CHARS={MIN_FRAGMENT_CHARS}, '
          f'OCR_TRIGGER_MIN_CHARS={OCR_TRIGGER_MIN_CHARS}, DEDUPE_SHORT_MAX_CHARS={DEDUPE_SHORT_MAX_CHARS}')
    check('тестовые тексты подобраны по длине',
          len(LONG) > OCR_TRIGGER_MIN_CHARS and MIN_FRAGMENT_CHARS < len(HEADING) <= OCR_TRIGGER_MIN_CHARS
          and len(HEADER50) == OCR_TRIGGER_MIN_CHARS and len(TINY) <= MIN_FRAGMENT_CHARS,
          (len(LONG), len(HEADING), len(HEADER50), len(TINY)))

    rag = _rag()
    frags, long_chars = rag._page_fragments([LONG, HEADING, TINY, JUNK])
    check('длинный абзац и заголовок сохранены, «Вход Выход» и мусор — нет',
          frags == [LONG, HEADING], frags)
    check('long_chars считает только длинные блоки (заголовок не в счёт)',
          long_chars == len(LONG), (long_chars, len(LONG)))

    frags, long_chars = rag._page_fragments([HEADER50])
    check('50-символьный колонтитул СОХРАНЁН как фрагмент', frags == [HEADER50], frags)
    check('но long_chars == 0 → OCR для такой страницы всё равно запустится', long_chars == 0, long_chars)

    frags, _ = rag._page_fragments(['первая строка\nвторая строка блока целиком'])
    check('многострочный блок склеен в один пробел',
          frags == ['первая строка вторая строка блока целиком'], frags)

    frags, _ = rag._page_fragments(['', '   ', None, JUNK])
    check('пустые блоки и мусор дают пустой список', frags == [], frags)

    order = [HEADING, LONG, 'Рекомендации по подключению к трубопроводу']
    frags, _ = rag._page_fragments(order)
    check('порядок фрагментов на странице сохраняется', frags == order, frags)


def test_dedupe_key():
    print('\n2) _dedupe_key: что дедуплицируется')
    rag = _rag()
    check('короткий текст → нормализованный ключ',
          rag._dedupe_key('Руководство  по Монтажу и наладке') == 'руководство по монтажу и наладке',
          rag._dedupe_key('Руководство  по Монтажу и наладке'))
    check('длинный текст не дедуплицируется (None)',
          rag._dedupe_key('Слово ' * 40) is None)
    check(f'граница: {DEDUPE_SHORT_MAX_CHARS} симв. дедуплицируется, {DEDUPE_SHORT_MAX_CHARS + 1} — нет',
          rag._dedupe_key('я' * DEDUPE_SHORT_MAX_CHARS) is not None
          and rag._dedupe_key('я' * (DEDUPE_SHORT_MAX_CHARS + 1)) is None)


def test_docx(tmp):
    print('\n3) DOCX: порог и дедуп повторов-ячеек (через _process_file)')
    try:
        import docx
    except ImportError:
        print('  ⏭ python-docx недоступен — раздел пропущен')
        return
    doc = docx.Document()
    long_par = ('Дозировочная станция предназначена для точного дозирования реагента '
                'в поток воды и поставляется с расходомером.')
    # Пустые абзацы дают разделитель '\n\n', по которому код разбивает текст на фрагменты
    for par in [long_par, '', 'раструб d32 под склейку', '', 'раструб d32 под склейку',
                '', 'раструб d75 под склейку', '', 'Параметры', '', '99']:
        doc.add_paragraph(par)
    path = os.path.join(tmp, 'spec.docx')
    doc.save(path)

    rag = _rag()
    rag.settings = type('S', (), {'get': staticmethod(lambda key, default=None: default)})()
    all_knowledge = {}
    rag._process_file(path, all_knowledge)
    frags = all_knowledge.get(path) or []

    check('короткая ячейка спецификации («раструб d32 под склейку», 23 симв.) сохранена',
          any('раструб d32 под склейку' in f for f in frags), frags)
    check('повтор ячейки в файле оставлен один раз',
          sum(1 for f in frags if 'раструб d32 под склейку' in f) == 1, frags)
    bodies = [f.split('] ', 1)[1].strip() if '] ' in f else f for f in frags]
    check('«Параметры» (9 симв.) и «99» отброшены',
          'Параметры' not in bodies and '99' not in bodies, bodies)
    check('длинный абзац на месте', any(long_par[:40] in f for f in frags), frags)


def test_real_pdf():
    print('\n4) Реальный PDF из базы (если есть PyMuPDF)')
    try:
        import fitz  # noqa: F401
    except ImportError:
        print('  ⏭ PyMuPDF не установлен — интеграционная проверка пропущена (Windows: работает фолбэк PyPDF2)')
        return
    path = None
    for root, dirs, files in os.walk('Database'):
        for f in files:
            if f.startswith('21. Аэрационная колонна') and f.endswith('.pdf'):
                path = os.path.join(root, f)
    if not path:
        print('  ⏭ файла «21. Аэрационная колонна_print.pdf» в Database/ нет — проверка пропущена')
        return

    rag = _rag()
    rag.settings = rag_core.RAGSettings()          # реальные настройки (OCR и кэш страниц)
    frags = rag._extract_pdf_text(path)
    print(f'   файл: {os.path.basename(path)} → фрагментов {len(frags)}')

    # до правки фильтра (порог 50) извлекалось 191 фрагмент
    check('фрагментов стало больше, чем было при пороге 50 (191)', len(frags) > 191, len(frags))
    bodies = [f.split('] ', 1)[1] for f in frags if '] ' in f]
    check('нет фрагментов короче порога', min(len(b) for b in bodies) > MIN_FRAGMENT_CHARS,
          min(len(b) for b in bodies))
    header_hits = sum(1 for b in bodies if b.strip() == HEADER50)
    check('колонтитул «Аэрационная колонна | Паспорт…» ровно один (дедуп)', header_hits == 1, header_hits)
    check('текст с чертежа (OCR-кэш) на месте — OCR не отключился из-за коротких блоков',
          any('Напорная аэрация' in b for b in bodies), [b[:60] for b in bodies[:3]])


def main():
    tmp = tempfile.mkdtemp(prefix='frag_test_')
    test_page_fragments()
    test_dedupe_key()
    test_docx(tmp)
    test_real_pdf()
    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for name in FAILED:
        print(f'  ❌ {name}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
