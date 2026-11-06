# test_context_diversity.py - диверсификация контекста: не больше N фрагментов из одного файла
#
# Что проверяется (RAGCore._diversify_indices, настройка context_per_file_limit):
#   1) лимит на файл соблюдается, добор идёт из других файлов;
#   2) общее число фрагментов не превышает top_k;
#   3) context_per_file_limit = 0 → ограничения нет (прежнее поведение: срез top_k);
#   4) фрагментов меньше, чем top_k → возвращаются все, без исключений;
#   5) пустой список индексов → пустой результат;
#   6) ключа нет в настройках → работает дефолт (3), а мусорное значение не ломает поиск;
#   7) порядок внутри выдачи сохраняется по убыванию скора (индексы берутся в том же порядке).
#
# Причина фичи: в базе aquasegmentum 21 .txt каталога сайта (~48 тыс. строк, 30% из них
# служебные «Добавить отзыв»/«Артикул»/«N руб./шт») занимали весь top_k, и в контекст
# не попадали ни отзывы, ни паспорта, ни инструкции.
#
# Запуск из корня проекта:  OPENAI_API_KEY=test venv/Scripts/python.exe tests/test_context_diversity.py
#                           (Linux-сервер: OPENAI_API_KEY=test venv/bin/python tests/test_context_diversity.py)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


class _Settings:
    """Заглушка настроек: только get(), как у RAGSettings."""

    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


def _core(fragment_sources, **settings):
    """RAGCore без __init__: не тянем модель эмбеддингов и базу знаний."""
    from rag_core import RAGCore
    core = RAGCore.__new__(RAGCore)
    core.fragment_sources = fragment_sources
    core.settings = _Settings(**settings)
    return core


def test_limit_per_file():
    # 10 фрагментов одного файла идут первыми, затем по одному из других файлов
    src = ['/db/a.txt'] * 10 + ['/db/b.pdf', '/db/c.csv', '/db/d.txt']
    core = _core(src, context_per_file_limit=3)
    picked = core._diversify_indices(list(range(len(src))), 5)
    assert len(picked) == 5, picked
    from collections import Counter
    per = Counter(src[i] for i in picked)
    assert per['/db/a.txt'] == 3, per
    assert per['/db/b.pdf'] == 1 and per['/db/c.csv'] == 1, per
    # порядок сохранён: сначала лучшие из a.txt, потом остальные
    assert picked == [0, 1, 2, 10, 11], picked
    print("  ✅ лимит на файл соблюдён, добор из других файлов")


def test_top_k_respected():
    src = [f'/db/f{i}.txt' for i in range(20)]
    core = _core(src, context_per_file_limit=3)
    picked = core._diversify_indices(list(range(20)), 7)
    assert len(picked) == 7, picked
    print("  ✅ общее число фрагментов = top_k")


def test_disabled():
    src = ['/db/a.txt'] * 10 + ['/db/b.pdf']
    core = _core(src, context_per_file_limit=0)
    picked = core._diversify_indices(list(range(11)), 4)
    assert picked == [0, 1, 2, 3], picked  # прежнее поведение: просто срез
    core2 = _core(src, context_per_file_limit=-1)
    assert core2._diversify_indices(list(range(11)), 4) == [0, 1, 2, 3]
    print("  ✅ 0 / -1 = без ограничения (прежнее поведение)")


def test_fewer_than_top_k():
    src = ['/db/a.txt', '/db/a.txt', '/db/b.pdf']
    core = _core(src, context_per_file_limit=2)
    picked = core._diversify_indices([0, 1, 2], 10)
    assert picked == [0, 1, 2], picked
    print("  ✅ фрагментов меньше top_k — отдаём все")


def test_empty():
    core = _core([], context_per_file_limit=3)
    assert core._diversify_indices([], 5) == []
    print("  ✅ пустой вход — пустой результат")


def test_default_and_bad_value():
    src = ['/db/a.txt'] * 5 + ['/db/b.pdf']
    core = _core(src)  # ключа нет → дефолт 3
    picked = core._diversify_indices(list(range(6)), 4)
    from collections import Counter
    assert Counter(src[i] for i in picked)['/db/a.txt'] == 3, picked
    core_bad = _core(src, context_per_file_limit='мусор')
    picked_bad = core_bad._diversify_indices(list(range(6)), 4)
    assert Counter(src[i] for i in picked_bad)['/db/a.txt'] == 3, picked_bad
    print("  ✅ дефолт 3 при отсутствии ключа и при мусорном значении")


def test_source_missing():
    # защита от рассинхрона: источников меньше, чем фрагментов
    core = _core(['/db/a.txt'], context_per_file_limit=2)
    picked = core._diversify_indices([0, 1, 2], 5)
    assert picked == [0, 1, 2], picked
    print("  ✅ нет источника для индекса — не падаем")


if __name__ == '__main__':
    print("Диверсификация контекста (context_per_file_limit):")
    for fn in (test_limit_per_file, test_top_k_respected, test_disabled, test_fewer_than_top_k,
               test_empty, test_default_and_bad_value, test_source_missing):
        fn()
    print("Все проверки пройдены.")
