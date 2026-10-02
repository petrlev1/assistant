# test_image_recognition.py - картинки в базе знаний: текст + описание → <имя>.txt
#
# Проверяется распознавание изображений на подменённом requests.post (реальной сети нет):
#   1) ответ модели с маркерами ТЕКСТ:/ОПИСАНИЕ: → строки текста и описание;
#   2) ответ без маркеров (модель нарушила формат) → весь ответ идёт в описание;
#   3) _process_file на картинке без одноимённого .txt → один вызов API, создан <имя>.txt,
#      знания в индексе — без строк-комментариев '#';
#   4) картинка с готовым .txt → API НЕ вызывается (описания достаточно);
#   5) кэш: txt удалён, но описание уже распознавалось → API НЕ вызывается;
#   6) пустой ответ (message без content) → None, маркер в кэше, txt не создан;
#   7) HTTP 500 → None, txt не создан;
#   8) распознавание выключено / нет ключа → None без вызовов API;
#   9) группа документов у картинки — «🖼 Изображения»;
#  10) приоритет TXT: картинка не идёт в эмбеддинги, если рядом лежит .txt.
#
# Запуск из корня проекта:  venv/Scripts/python.exe tests/test_image_recognition.py   (Linux-сервер: venv/bin/python tests/test_image_recognition.py)
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# Запуск из любого каталога: корень проекта в sys.path (import rag_core)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        self.data = {'llm_api_key': 'test-key', 'image_model': 'qwen-vl-plus',
                     'image_base_url': 'https://example.invalid/compatible-mode/v1',
                     'image_max_side': 1600, 'image_enabled': True}
        self.data.update(over)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return self.data.get('llm_api_key', '')


class _Resp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {}, ensure_ascii=False)

    def json(self):
        return self._body


class _Post:
    """Подмена requests.post: отдаёт заранее заданные ответы по очереди и считает вызовы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.payloads = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.payloads.append({'url': url, 'json': json, 'headers': headers})
        return self.responses.pop(0)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _rag(**over):
    """RAGCore без конструктора: только настройки, БД/эмбеддинги не нужны."""
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.settings = _Settings(**over)
    rag.current_user_id = 1
    return rag


def _log_ctx():
    cap = _LogCapture()
    logger = logging.getLogger('rag_core')
    old_level = logger.level
    logger.addHandler(cap)
    logger.setLevel(logging.INFO)
    return cap, (lambda: (logger.removeHandler(cap), logger.setLevel(old_level)))


PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64   # «картинка»: настоящий файл не нужен


def _make_image(tmp, name):
    # Содержимое уникально для каждого имени: кэш распознавания считается по md5 файла,
    # и с одинаковыми байтами второй файл получил бы ответ первого.
    p = os.path.join(tmp, name)
    with open(p, 'wb') as f:
        f.write(PNG_BYTES + name.encode('utf-8'))
    return p


ANSWER = ('ТЕКСТ:\n'
          'АКВАСЕГМЕНТ\n'
          'Клапан обратный d32, арт. 4512\n'
          'нет\n'
          'ОПИСАНИЕ:\n'
          'Фото обратного клапана: корпус серого цвета, на корпусе шильдик с артикулом.')


def main():
    tmp = tempfile.mkdtemp(prefix='image_test_')

    # 1) разбор ответа с маркерами
    print('\n1) Разбор ответа модели (ТЕКСТ:/ОПИСАНИЕ:)')
    lines, desc = rag_core.RAGCore._split_image_answer(ANSWER)
    check('текст разобран построчно', lines == ['АКВАСЕГМЕНТ', 'Клапан обратный d32, арт. 4512'], lines)
    check('строка «нет» отброшена', 'нет' not in lines, lines)
    check('описание — одна строка', desc.startswith('Фото обратного клапана') and '\n' not in desc, desc)

    # 2) модель нарушила формат
    print('\n2) Ответ без маркеров — весь ответ считается описанием')
    lines, desc = rag_core.RAGCore._split_image_answer('Просто описание картинки без маркеров')
    check('текст пуст', lines == [], lines)
    check('описание = ответ', desc == 'Просто описание картинки без маркеров', desc)

    # 3) распознавание при индексации: txt создаётся и факты попадают в базу знаний
    print('\n3) _process_file на картинке без .txt')
    cap, restore = _log_ctx()
    post = _Post([_Resp(200, {'choices': [{'message': {'content': ANSWER}}]})])
    rag_core.requests.post = post
    img = _make_image(tmp, 'klapan.png')
    knowledge = {}
    _rag()._process_file(img, knowledge)
    restore()
    txt = os.path.splitext(img)[0] + '.txt'
    check('API вызван один раз', post.calls == 1, post.calls)
    check('картинка ушла картинкой в data:URL',
          'data:image/' in json.dumps(post.payloads[0]['json'], ensure_ascii=False), post.payloads[0]['json'])
    check('создан <имя>.txt', os.path.exists(txt))
    body = open(txt, encoding='utf-8').read()
    check('в txt есть описание', 'На изображении klapan.png изображено:' in body, body[:200])
    check('в txt есть текст с картинки', 'Клапан обратный d32, арт. 4512' in body, body[:200])
    facts = knowledge.get(txt, [])
    check('знания в индексе есть', len(facts) == 3, facts)
    check('строки-комментарии # в индекс не попали', not [f for f in facts if f.startswith('#')], facts)
    check('в логе нет ERROR', not [r for r in cap.records if r[0] == 'ERROR'], cap.records)

    # 4) описание уже есть — API не вызывается
    print('\n4) Картинка с готовым .txt — распознавание не повторяется')
    post = _Post([])
    rag_core.requests.post = post
    knowledge = {}
    _rag()._process_file(img, knowledge)
    check('API не вызван', post.calls == 0, post.calls)
    check('знаний по картинке нет (их даёт .txt)', knowledge == {}, knowledge)
    check('приоритет TXT: картинка отброшена из эмбеддингов',
          _rag()._apply_txt_priority([img, txt]) == [txt],
          _rag()._apply_txt_priority([img, txt]))

    # 5) кэш: txt удалён, но описание уже распознавалось
    print('\n5) Кэш распознавания (txt удалён, файл тот же)')
    os.remove(txt)
    post = _Post([])
    rag_core.requests.post = post
    knowledge = {}
    _rag()._process_file(img, knowledge)
    check('API не вызван (взято из кэша)', post.calls == 0, post.calls)
    check('txt восстановлен из кэша', os.path.exists(txt))

    # 6) пустой ответ модели
    print('\n6) Пустой ответ (message без content)')
    cap, restore = _log_ctx()
    post = _Post([_Resp(200, {'choices': [{'message': {'role': 'assistant'}, 'finish_reason': 'stop'}]})])
    rag_core.requests.post = post
    img_empty = _make_image(tmp, 'pustoe.png')
    out = _rag()._describe_image(img_empty)
    restore()
    check('вернулся None', out is None, out)
    check('в логе нет ERROR', not [r for r in cap.records if r[0] == 'ERROR'], cap.records)
    check('txt не создан', not os.path.exists(os.path.splitext(img_empty)[0] + '.txt'))

    # 7) ошибка API
    print('\n7) HTTP 500')
    cap, restore = _log_ctx()
    post = _Post([_Resp(500, None, text='internal error')])
    rag_core.requests.post = post
    img_err = _make_image(tmp, 'oshibka.jpg')
    out = _rag()._describe_image(img_err)
    restore()
    check('вернулся None', out is None, out)
    check('ошибка в логе', any(r[0] == 'ERROR' for r in cap.records), cap.records)
    check('txt не создан', not os.path.exists(os.path.splitext(img_err)[0] + '.txt'))

    # 8) выключено / нет ключа
    print('\n8) Распознавание выключено и «нет ключа»')
    post = _Post([])
    rag_core.requests.post = post
    img_off = _make_image(tmp, 'off.png')
    check('выключено → None без вызова API',
          _rag(image_enabled=False)._describe_image(img_off) is None and post.calls == 0, post.calls)
    check('нет ключа → None без вызова API',
          _rag(llm_api_key='')._describe_image(img_off) is None and post.calls == 0, post.calls)

    # 9) группа документов
    print('\n9) Группа документов')
    check('картинка → «🖼 Изображения»',
          rag_core.detect_doc_group('Габариты бака.png') == '🖼 Изображения',
          rag_core.detect_doc_group('Габариты бака.png'))
    check('текстовый файл — прежняя логика',
          rag_core.detect_doc_group('Инструкция.txt') == '📖 Инструкции')

    print(f"\nИтог: OK {len(PASSED)}, FAIL {len(FAILED)}")
    if FAILED:
        print('Провалено:')
        for name in FAILED:
            print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
