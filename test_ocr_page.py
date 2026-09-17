# test_ocr_page.py - разбор ответов OCR (qwen-vl-ocr): пустая страница ≠ ошибка
#
# Проверяется _ocr_page на подменённом requests.post:
#   1) обычный ответ с текстом → текст возвращён и закэширован;
#   2) страница без текста (200, в message НЕТ поля content, как на пустой форме) →
#      None, в логе INFO (не ERROR), в кэш пишется ПУСТОЙ файл-маркер;
#   3) повторный вызов с пустым маркером → API НЕ вызывается (экономия денег);
#   4) content списком частей → части склеены;
#   5) HTTP 500 и 200 без choices → None, ошибка в логе, кэш НЕ пишется.
#
# Запуск из папки проекта:  venv/Scripts/python.exe test_ocr_page.py
import json
import logging
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import rag_core

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  -> {extra!r}' if not cond and extra != '' else ''))


class _Settings:
    """Заглушка RAGSettings."""

    def __init__(self, **over):
        self.data = {'llm_api_key': 'test-key', 'ocr_model': 'qwen-vl-ocr',
                     'ocr_base_url': 'https://example.invalid/compatible-mode/v1', 'ocr_dpi': 150}
        self.data.update(over)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return self.data.get('llm_api_key', '')


class _Pix:
    def tobytes(self, fmt='png'):
        return b'\x89PNG\r\n\x1a\n' + b'fake-image'


class _Page:
    def get_pixmap(self, dpi=150):
        return _Pix()


class _Resp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        # как у настоящего ответа: .text — сырое тело (иначе логи нечего печатать)
        self.text = text if text is not None else json.dumps(body or {}, ensure_ascii=False)

    def json(self):
        return self._body


class _Post:
    """Подмена requests.post: отдаёт заранее заданные ответы по очереди и считает вызовы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _rag():
    """RAGCore без конструктора: только настройки, БД/эмбеддинги не нужны."""
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.settings = _Settings()
    rag.current_user_id = 1
    return rag


def _log_ctx():
    cap = _LogCapture()
    logger = logging.getLogger('rag_core')
    old_level = logger.level
    logger.addHandler(cap)
    logger.setLevel(logging.INFO)
    return cap, (lambda: (logger.removeHandler(cap), logger.setLevel(old_level)))


def main():
    tmp = tempfile.mkdtemp(prefix='ocr_test_')

    # 1) нормальный ответ с текстом
    print('\n1) Страница с текстом')
    cap, restore = _log_ctx()
    post = _Post([_Resp(200, {'choices': [{'message': {'content': 'распознанный текст страницы'}}]})])
    rag_core.requests.post = post
    cache = os.path.join(tmp, 'page_2.txt')
    out = _rag()._ocr_page(_Page(), 'doc.pdf', 1, __import__('pathlib').Path(cache))
    restore()
    check('текст возвращён', out == 'распознанный текст страницы', out)
    check('текст закэширован', os.path.exists(cache) and open(cache, encoding='utf-8').read() == out)
    check('в логе нет ошибок', not [r for r in cap.records if r[0] == 'ERROR'], cap.records)

    # 2) пустая страница: 200, message без content (реальный ответ qwen-vl-ocr на пустой форме)
    print('\n2) Страница без текста (message без поля content) — как было на стр. 28')
    cap, restore = _log_ctx()
    post = _Post([_Resp(200, {'choices': [{'message': {'reasoning_content': '', 'role': 'assistant'},
                                            'finish_reason': 'stop', 'index': 0}],
                            'usage': {'completion_tokens': 1}})])
    rag_core.requests.post = post
    cache = os.path.join(tmp, 'page_28.txt')
    out = _rag()._ocr_page(_Page(), 'doc.pdf', 27, __import__('pathlib').Path(cache))
    restore()
    check('возвращён None (не исключение, не «текст»)', out is None, out)
    check('в логе нет ERROR', not [r for r in cap.records if r[0] == 'ERROR'], cap.records)
    check('в логе есть понятное INFO про пустую страницу',
          any(r[0] == 'INFO' and 'не распознал текст' in r[1] for r in cap.records), cap.records)
    check('записан пустой файл-маркер', os.path.exists(cache) and open(cache, encoding='utf-8').read() == '')

    # 3) повторный вызов с маркером: API не вызывается
    print('\n3) Повторная загрузка базы: API не дёргается')
    post = _Post([])          # любой вызов упал бы IndexError
    rag_core.requests.post = post
    out = _rag()._ocr_page(_Page(), 'doc.pdf', 27, __import__('pathlib').Path(cache))
    check('API не вызван (0 запросов)', post.calls == 0, post.calls)
    check('пустой результат → None (страница пропускается)', not out, out)

    # 4) content списком частей
    print('\n4) content списком частей')
    post = _Post([_Resp(200, {'choices': [{'message': {'content': [{'type': 'text', 'text': 'первая часть'},
                                                                   {'type': 'text', 'text': 'вторая часть'}]}}]})])
    rag_core.requests.post = post
    cache = os.path.join(tmp, 'page_3.txt')
    out = _rag()._ocr_page(_Page(), 'doc.pdf', 2, __import__('pathlib').Path(cache))
    check('части склеены', out == 'первая часть\nвторая часть', out)

    # 5) реальные ошибки API
    print('\n5) Ошибки API: HTTP 500 и ответ без choices')
    cap, restore = _log_ctx()
    post = _Post([_Resp(500, None, text='internal error'),
                  _Resp(200, {'code': 'InvalidParameter', 'message': 'bad image'})])
    rag_core.requests.post = post
    c500 = os.path.join(tmp, 'page_500.txt')
    out500 = _rag()._ocr_page(_Page(), 'doc.pdf', 499, __import__('pathlib').Path(c500))
    cno = os.path.join(tmp, 'page_nochoice.txt')
    outno = _rag()._ocr_page(_Page(), 'doc.pdf', 500, __import__('pathlib').Path(cno))
    restore()
    check('HTTP 500 → None, кэш не создан', out500 is None and not os.path.exists(c500))
    check('ответ без choices → None, кэш не создан', outno is None and not os.path.exists(cno))
    errors = [r for r in cap.records if r[0] == 'ERROR']
    check('оба случая попали в лог как ERROR', len(errors) == 2, cap.records)
    check('в ERROR про отсутствие choices видно тело ответа',
          any('choices' in r[1] and 'InvalidParameter' in r[1] for r in errors), errors)

    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for name in FAILED:
        print(f'  ❌ {name}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
