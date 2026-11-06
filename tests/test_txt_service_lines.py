# test_txt_service_lines.py - фильтр служебных строк выгрузки каталога сайта (.txt)
#
# Что проверяется (rag_core._is_service_catalog_line / _is_catalog_dump_path):
#   1) навигация выгрузки отбрасывается («Купить … в Москве или с доставкой: …», «Похожие товары (8): …»);
#   2) служебные подписи отбрасываются («Добавить отзыв», «Отзывов: 0», «Артикул: X», «В корзину»);
#   3) ФАКТЫ остаются: «<Товар>: Рабочее давление, бар», «<Товар>: 3–8», проза, цена;
#   4) рукописные .txt не считаются выгрузкой (фильтр к ним не применяется) — иначе
#      «Ключ: значение» формата БЗ («Скорость фильтрации м/час: 10-14») улетало бы в мусор;
#   5) _process_file: site_*.txt режется, обычный .txt — нет; заголовки «# …» не индексируются.
#
# Зачем: в базе aquasegmentum 21 такой файл давал 47 276 строк, из них 27 692 (59%) — служебные.
# Они повторяют название товара и выигрывают у осмысленных фрагментов по BM25.
#
# Запуск из корня проекта:  OPENAI_API_KEY=test venv/Scripts/python.exe tests/test_txt_service_lines.py
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def test_dump_path_detection():
    from rag_core import _is_catalog_dump_path
    assert _is_catalog_dump_path('Database/user_2/site_aquasegment.ru_catalog_kartridzh_3ae1c6.txt')
    assert _is_catalog_dump_path('C:/x/SITE_example.com_page.txt')
    assert not _is_catalog_dump_path('Database/user_2/термочехлы_aquasegmentum.txt')
    assert not _is_catalog_dump_path('Database/user_2/Заметки.txt')
    assert not _is_catalog_dump_path('Database/user_2/iron filter описание.txt')
    print("  ✅ выгрузкой сайта считаются только site_*.txt")


def test_navigation_dropped():
    from rag_core import _is_service_catalog_line as svc
    lines = [
        'Купить Мембранный элемент VONTRON-1812 (75 G\\D) в Москве или с доставкой: sale@aquasegment.ru',
        'Купить Корпус фильтра 1252 HY N в Москве или с доставкой: Специальное предложение в феврале',
        'Мембранный элемент VONTRON-1812 (75 G\\D) заказать для систем водоочистки в Аквасегментум. Характеристики',
        'Мы на портале поставщиков',
        'Наши новости',
        'Похожие товары (8): Фитинг прямой РР для WS1',
        'Широкий ассортимент Аниониты для очистки воды и водоподготовки в интернет-магазине Аквасегмент.',
    ]
    for line in lines:
        assert svc(line), line
    print("  ✅ навигация выгрузки отбрасывается")


def test_service_labels_dropped():
    from rag_core import _is_service_catalog_line as svc
    lines = [
        'Мембранный элемент VONTRON-1812 (75 G\\D): Добавить отзыв',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Отзывов: 0',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Артикул: VONTRON-1812 (75 G\\D)',
        'Мембранный элемент VONTRON-1812 (75 G\\D): В корзину',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Все характеристики',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Описание товара:',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Проконсультировать',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Рассчитать доставку',
        'Мембранный элемент VONTRON-1812 (75 G\\D): В наличии',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Скидки постоянным покупателям',
        'Термочехол Aquasegmentum Standart: Купить в один клик',
        'Корпус фильтра 1465 HY: Страна производства: Россия',
    ]
    for line in lines:
        assert svc(line), line
    print("  ✅ служебные подписи отбрасываются")


def test_facts_kept():
    from rag_core import _is_service_catalog_line as svc
    keep = [
        'Мембранный элемент VONTRON-1812 (75 G\\D): Рабочее давление, бар',
        'Мембранный элемент VONTRON-1812 (75 G\\D): 3–8',
        'Мембранный элемент VONTRON-1812 (75 G\\D): Рабочая температура, °С',
        'Мембранный элемент VONTRON-1812 (75 G\\D): 1 168.31 руб. / шт',
        'Мембранный элемент VONTRON-1812 (75 G\\D) 1 168.31 руб. / шт',
        'Мембраны ультрафильтрации имеют маленькие поры, которые задерживают нежелательные вещества.',
        'Комплект умягчителя 1354 F69A3: 2,1 - 2,6 м3/ч',
        'V3010-15H Инжектор H orange 24"/ 1.5" 1 669.42 руб. / шт',
    ]
    for line in keep:
        assert not svc(line), line
    print("  ✅ факты (характеристики, значения, цены, проза) остаются")


def test_handwritten_kv_kept():
    from rag_core import _is_service_catalog_line as svc
    # Формат БЗ «один факт на строку» — такие строки удалять нельзя
    keep = [
        'Скорость фильтрации м/час: 10-14',
        'Диапазон Ph: 6,8-8,5',
        '- Email: sale@aquasegment.ru',
        '- **Регенерация**: Таблетированная соль (NaCl).',
        'Наличие хлора: Допускается',
    ]
    for line in keep:
        assert not svc(line), line
    print("  ✅ рукописные «Ключ: значение» не считаются служебными")


def test_empty_line():
    from rag_core import _is_service_catalog_line as svc
    assert svc(''), 'пустая строка'
    assert svc('   '), 'пробелы'
    print("  ✅ пустые строки отбрасываются")


def test_process_file_gate():
    """Фильтр применяется к site_*.txt и НЕ применяется к обычным .txt."""
    from rag_core import RAGCore
    core = RAGCore.__new__(RAGCore)
    with tempfile.TemporaryDirectory() as tmp:
        dump = os.path.join(tmp, 'site_example.com_katalog.txt')
        manual = os.path.join(tmp, 'Памятка.txt')
        content_dump = (
            "# Заголовок не индексируется\n"
            "Купить Клапан в Москве или с доставкой: Наши новости\n"
            "Клапан WS1: Добавить отзыв\n"
            "Клапан WS1: Артикул: V3007\n"
            "Клапан WS1: Расход, м3/ч\n"
            "Клапан WS1: 2,5\n"
        )
        content_manual = (
            "# Памятка\n"
            "Расход, м3/ч: 2,5\n"
            "Артикул: V3007\n"
        )
        for path, text in ((dump, content_dump), (manual, content_manual)):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        all_knowledge = {}
        core._process_file(dump, all_knowledge)
        core._process_file(manual, all_knowledge)
        dump_lines = all_knowledge[dump]
        manual_lines = all_knowledge[manual]
        assert dump_lines == ['Клапан WS1: Расход, м3/ч', 'Клапан WS1: 2,5'], dump_lines
        assert manual_lines == ['Расход, м3/ч: 2,5', 'Артикул: V3007'], manual_lines
    print("  ✅ _process_file: фильтр только для site_*.txt, заголовки не индексируются")


if __name__ == '__main__':
    print("Служебные строки выгрузки каталога (.txt):")
    for fn in (test_dump_path_detection, test_navigation_dropped, test_service_labels_dropped,
               test_facts_kept, test_handwritten_kv_kept, test_empty_line, test_process_file_gate):
        fn()
    print("Все проверки пройдены.")
