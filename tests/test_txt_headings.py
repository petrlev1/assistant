# -*- coding: utf-8 -*-
"""Тесты контекстных префиксов из заголовков .txt (правка индексатора, вариант B).

Проблема, которую закрываем: строки-заголовки «# Раздел» выбрасывались из индекса, поэтому
описание серии распадалось на короткие фрагменты без имени серии и не находилось на обзорный
вопрос («Какие виды термочехлов у вас есть?»). Теперь заголовок становится префиксом
контекста для строк своего раздела.

Проверяется:
  1) заголовок → префикс `[Заголовок] ` у последующих строк, сам заголовок не фрагмент;
  2) служебные шапки (URL, «Сайт —», «Файл:», «Модель:», авто-описание картинки) префиксом
     не становятся — иначе они висят в каждом фрагменте и засоряют лексический поиск;
  3) смена раздела переключает префикс; файлы без заголовков индексируются как раньше;
  4) длинный заголовок обрезается по слову до HEADING_PREFIX_MAX_CHARS;
  5) интеграция: _process_file на реальном .txt даёт префиксованные фрагменты, а описание
     изображения (шапка из «#» + текст OCR) — нет.

Модель/сеть/БД не нужны: ветка .txt в _process_file только читает файл.
Запуск из корня проекта:  venv/Scripts/python.exe tests/test_txt_headings.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import rag_core

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    mark = 'OK  ' if cond else 'FAIL'
    line = f'  [{mark}] {name}'
    if not cond and extra != '':
        line += f'  -> {extra!r}'
    print(line)


H = rag_core._heading_text
F = rag_core._txt_fragments


def test_heading_text():
    print('\n1) Текст заголовка')
    check('обычный заголовок сохраняется',
          H('# Термочехлы Aquasegmentum Серия Стандарт — однослойные термочехлы.') ==
          'Термочехлы Aquasegmentum Серия Стандарт — однослойные термочехлы.',
          H('# Термочехлы Aquasegmentum Серия Стандарт — однослойные термочехлы.'))
    check('решётки снимаются (## тоже)', H('### Серия Premium') == 'Серия Premium', H('### Серия Premium'))
    check('не заголовок → пусто', H('Термочехол BB10 габариты 20х30 см') == '')
    check('«# Сайт — https://…» не заголовок', H('# Сайт — https://aquasegment.ru/') == '')
    check('«# Файл: …» не заголовок', H('# Файл: 100.jpg') == '')
    check('«# Модель: …» не заголовок', H('# Модель: qwen-vl-plus · 2026-11-02 21:00') == '')
    check('авто-описание картинки не заголовок',
          H('# Описание изображения — создано автоматически (RAGSTONE)') == '')
    check('«# Текст на изображении …» не заголовок',
          H('# Текст на изображении (распознан дословно):') == '')
    check('пустой заголовок «#» → пусто', H('#') == '')
    check('www-адрес отрезается, слово остаётся', H('# Каталог www.aquasegment.ru') == 'Каталог',
          H('# Каталог www.aquasegment.ru'))
    check('название страницы каталога сохраняется, URL-хвост отрезается',
          H('# Купить Стерилизатор UV-12 GPM (SDE-055) в Москве или с доставкой — '
            'https://aquasegment.ru/catalog/x/y/') ==
          'Купить Стерилизатор UV-12 GPM (SDE-055) в Москве или с доставкой',
          H('# Купить Стерилизатор UV-12 GPM (SDE-055) в Москве или с доставкой — '
            'https://aquasegment.ru/catalog/x/y/'))
    check('«# Сайт — URL» остаётся служебным', H('# Сайт — https://x.ru/catalog/') == '',
          H('# Сайт — https://x.ru/catalog/'))
    check('[Метаданные] — служебный', H('## [Метаданные]') == '', H('## [Метаданные]'))
    check('[Категория: …] — полезный заголовок',
          H('## [Категория: Многокомпонентные загрузки (Мультизагрузки)]') ==
          '[Категория: Многокомпонентные загрузки (Мультизагрузки)]',
          H('## [Категория: Многокомпонентные загрузки (Мультизагрузки)]'))

    long_head = '# ' + ' '.join(['Очень длинный заголовок раздела'] * 10)
    got = H(long_head)
    check('длинный заголовок обрезан по лимиту',
          0 < len(got) <= rag_core.HEADING_PREFIX_MAX_CHARS + 1, len(got))
    check('обрезанный заголовок заканчивается многоточием', got.endswith('…'), got[-20:])


def test_fragments():
    print('\n2) Сборка фрагментов с префиксом раздела')
    frags = F([
        '# Термочехлы Aquasegmentum — общая информация',
        'Термочехлы Aquasegmentum изготовлены из хлоропренового каучука.',
        '',
        '# Термочехлы Aquasegmentum Серия Стандарт — однослойные',
        'Термочехлы серии Стандарт имеют однослойную конструкцию.',
        'Термочехол серии Стандарт размер BB10 габариты: 20х30 см.',
        '# Серия Premium — двухслойные',
        'Термочехлы серии Premium имеют двухслойный материал.',
    ])
    check('фрагментов столько же, сколько содержательных строк', len(frags) == 4, frags)
    check('заголовок не стал отдельным фрагментом',
          'Термочехлы Aquasegmentum — общая информация' not in frags
          and 'Термочехлы Aquasegmentum Серия Стандарт — однослойные' not in frags, frags)
    check('первая строка получила префикс своего раздела',
          frags[0].startswith('[Термочехлы Aquasegmentum — общая информация]'), frags[0])
    check('строки серии Стандарт несут имя серии дважды (в префиксе и в тексте)',
          frags[1].count('Стандарт') >= 2 and frags[2].count('Стандарт') >= 2, frags[1:3])
    check('смена раздела переключает префикс',
          frags[3].startswith('[Серия Premium — двухслойные]'), frags[3])
    check('пустые строки пропущены', all(f.strip() for f in frags), frags)

    plain = F(['Первая строка', 'Вторая строка'])
    check('файл без заголовков индексируется как раньше', plain == ['Первая строка', 'Вторая строка'], plain)

    mixed = F(['# Сайт — https://x.ru', 'Полезный факт про фильтры.'])
    check('служебная шапка не даёт префикса', mixed == ['Полезный факт про фильтры.'], mixed)


def test_process_file():
    print('\n3) _process_file: реальный .txt и файл авто-описания картинки')
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)   # ветка .txt модель не трогает

    with tempfile.TemporaryDirectory() as d:
        kb = os.path.join(d, 'термочехлы_тест.txt')
        with open(kb, 'w', encoding='utf-8') as f:
            f.write('# Термочехлы Aquasegmentum Серия Стандарт — однослойные\n'
                    'Термочехол серии Стандарт размер BB10 габариты: 20х30 см.\n'
                    'Термочехол серии Стандарт размер BB20 габариты: 20х30 см.\n')
        all_knowledge = {}
        rag._process_file(kb, all_knowledge)
        frags = all_knowledge.get(kb)
        check('файл прочитан', bool(frags), all_knowledge)
        check('обе строки получили префикс с названием серии',
              frags and all('Серия Стандарт' in x for x in frags), frags)
        check('заголовок в список фрагментов не попал',
              frags and len(frags) == 2, frags)

        img = os.path.join(d, '100.txt')
        with open(img, 'w', encoding='utf-8') as f:
            f.write('# Описание изображения — создано автоматически (RAGSTONE)\n'
                    '# Файл: 100.jpg\n'
                    '# Модель: qwen-vl-plus · 2026-11-02 21:00\n'
                    '\n'
                    'На изображении 100.jpg изображено: корпус фильтра в термочехле.\n'
                    '# Текст на изображении (распознан дословно):\n'
                    'AQUASEGMENTUM BB10\n')
        img_knowledge = {}
        rag._process_file(img, img_knowledge)
        img_frags = img_knowledge.get(img) or []
        check('описание картинки прочитано', len(img_frags) == 2, img_frags)
        check('служебные шапки не превратились в префиксы',
              all(not x.startswith('[') for x in img_frags), img_frags)


def main():
    test_heading_text()
    test_fragments()
    test_process_file()
    total = len(PASSED) + len(FAILED)
    print(f'\nИтого: {len(PASSED)}/{total} проверок пройдено')
    if FAILED:
        print('Провалены:')
        for name in FAILED:
            print(f'  - {name}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
