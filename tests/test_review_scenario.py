# -*- coding: utf-8 -*-
"""Тесты сценария «ответ на отзыв» (слой 1: чеклист + примеры из базы знаний).

Что проверяется:
  1) детект задачи «написать ответ на отзыв» и тип претензии по лексике;
  2) сборка блока: чеклист по типу, подбор примеров того же типа, качество примеров,
     лимит количества, отсутствие примеров другого знака (положительный/негативный);
  3) интеграция в RAGCore.ask_model: блок идёт ПЕРВЫМ в контексте, правило попадает
     в системный промт, точные пути (прайс, ✏️-исправление) не перехватывают задачу,
     файлы-примеры попадают в «Источники»;
  4) обычный вопрос ведёт себя как раньше (сценарий не включается).

Сеть и LLM не нужны: requests.post и поиск по БЗ подменяются заглушками.
Запуск из корня проекта:  venv/Scripts/python.exe tests/test_review_scenario.py
"""
import csv
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import rag_core
import review_scenario

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    mark = 'OK  ' if cond else 'FAIL'
    line = f'  [{mark}] {name}'
    if not cond and extra != '':
        line += f'  -> {extra!r}'
    print(line)


# === Фикстура: база знаний пользователя с примерами отзывов =================

REVIEW_ROWS = [
    ("Мембрана A", "Оказался бракованный, не рабочий. Пришлось выбросить",
     "Здравствуйте! Спасибо за отзыв, нам жаль, что так вышло. Уточните, пожалуйста, "
     "в чём именно проявляется дефект, и вышлите фото товара и номер заказа — разберёмся "
     "и предложим замену."),
    ("Картридж B", "Не работает, течёт из-под крышки",
     "Добрый день! Извините за доставленные неудобства. Уточните, пожалуйста, где именно "
     "течёт, и пришлите фото узла — подскажем, что делать, или заменим."),
    ("Насос C", "Курьер опоздал на три дня, ждал долго", "Приносим извинения за задержку доставки. "
     "Уточните номер заказа — проверим статус и назовём точный срок."),
    ("Термочехол D", "Отличный товар, спасибо большое, всё понравилось",
     "Здравствуйте! Благодарим за отзыв, рады, что всё понравилось. Будем рады видеть вас снова."),
    ("Фильтр E", "Сломался через месяц, не держит давление",
     "Деталь соответствует по своим техническим характеристикам всем заявленным параметрам, "
     "согласно инструкции по эксплуатации."),
]

TXT_TEMPLATE = "Здравствуйте! Спасибо за отзыв, мы рады, что вы остались довольны!"


def make_user_dir():
    user_id = 900000 + (uuid.uuid4().int % 90000)
    folder = os.path.join("Database", f"user_{user_id}")
    os.makedirs(folder, exist_ok=True)
    csv_path = os.path.join(folder, "Задачи - Отзывы - Sheet1.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Товар", "Отзыв", "Ответ"])
        for row in REVIEW_ROWS:
            w.writerow(row)
    with open(os.path.join(folder, "Позитивные ответы на отзывы.txt"), "w", encoding="utf-8") as f:
        f.write(TXT_TEMPLATE + "\n")
    return user_id, folder


TASK_REVIEW = ("Ответь на отзыв по товару:\nМембранный элемент VONTRON-1812 (75 G\\D)\n"
               "Комментарий: Оказался бракованный, не рабочий. Пришлось просто выбросить, "
               "деньги на ветер")

TASK_POSITIVE = "Отзыв: Отличный товар, спасибо большое, всё понравилось, рекомендую"


def test_detect():
    print('\n1) Детект задачи и тип претензии')
    det = review_scenario.detect_review_task(TASK_REVIEW)
    check('задача «ответь на отзыв по товару…» распознана', det is not None, det)
    check('тип претензии — брак', det and det['type'] == 'брак', det)
    check('в признаках есть слово-отзыв', det and 'слово-отзыв' in det['signals'], det)

    det = review_scenario.detect_review_task(TASK_POSITIVE)
    check('положительный отзыв распознан', det is not None and det['type'] == 'положительный', det)

    det = review_scenario.detect_review_task(
        'Оказался бракованный, не рабочий. Пришлось просто выбросить, деньги на ветер')
    check('вставленный текст отзыва без слова «отзыв» распознан', det is not None, det)
    check('тип — брак', det and det['type'] == 'брак', det)

    det = review_scenario.detect_review_task('Сколько стоит мембранный элемент VONTRON-1812?')
    check('обычный товарный вопрос не считается отзывом', det is None, det)

    det = review_scenario.detect_review_task('Почему мембрана не работает?')
    check('вопрос про дефект не считается задачей-отзывом', det is None, det)

    det = review_scenario.detect_review_task('Ответь клиенту, у него сломался насос')
    check('«ответь клиенту + поломка» — сценарий', det is not None, det)

    print('\n1b) Классификация типов претензий')
    cases = [
        ('Товар пришёл разбитый, корпус треснул', 'повреждение'),
        ('Курьер опоздал, доставка задержалась на неделю', 'доставка'),
        ('Не подошёл по диаметру, не встал в систему', 'не подошёл'),
        ('Брак, не работает, пришлось выбросить', 'брак'),
        ('Слишком дорого, завышенная цена', 'цена'),
        ('Менеджер нахамил и не отвечает', 'обслуживание'),
        ('Отличный товар, спасибо', 'положительный'),
        ('Всё как в описании', 'общий'),
        ('Товар не понравился', 'общий'),
    ]
    for text, expect in cases:
        got = review_scenario.classify_complaint(text)
        check(f'{text[:38]!r} → {expect}', got == expect, got)


def test_block(folder):
    print('\n2) Блок сценария: чеклист, примеры, лимиты')
    block, sources = review_scenario.build_block(folder, TASK_REVIEW, max_examples=3)
    check('блок собран', bool(block), block[:80])
    check('в блоке есть заголовок сценария', 'СЦЕНАРИЙ: ОТВЕТ НА ОТЗЫВ' in block, block[:80])
    check('тип претензии в заголовке', 'брак' in block.split('\n')[0], block.split('\n')[0])
    check('чеклист просит фото и номер заказа',
          'Попросить фото товара и номер заказа' in block, block)
    check('запрет на «все товары сертифицированы»',
          'сертифицированы' in block, block)
    check('примеры берутся из CSV и попадают в источники',
          sources and sources[0].endswith('.csv'), sources)
    check('в блоке есть пример ответа про брак',
          'в чём именно проявляется дефект' in block, block)
    check('положительный пример в негативную задачу не попал',
          'рады, что всё понравилось' not in block.split('Примеры ответов')[0]
          and 'Будем рады видеть вас снова' not in block, block[-400:])

    one, _ = review_scenario.build_block(folder, TASK_REVIEW, max_examples=1)
    check('лимит примеров соблюдается (max_examples=1)',
          one.count('Ответ: «') == 1, one.count('Ответ: «'))

    pos, pos_sources = review_scenario.build_block(folder, TASK_POSITIVE, max_examples=3)
    check('положительная задача — положительные примеры',
          'Спасибо за отзыв, мы рады' in pos and 'в чём именно проявляется дефект' not in pos,
          pos[-300:])
    check('источник положительных примеров указан', bool(pos_sources), pos_sources)

    empty, empty_sources = review_scenario.build_block(
        os.path.join('Database', 'user_000000_missing'), TASK_REVIEW)
    check('без файлов примеров чеклист всё равно отдаётся', bool(empty) and not empty_sources,
          empty_sources)

    question, sources_q = review_scenario.build_block(folder, 'Сколько стоит VONTRON-1812?')
    check('товарный вопрос — пустой блок', question == '' and sources_q == [], question[:60])

    # Кэш: повторный вызов по неизменённым файлам возвращает те же данные
    a = review_scenario.load_examples(folder)
    b = review_scenario.load_examples(folder)
    check('кэш примеров работает', a is b and len(a) >= 5, (len(a), len(b)))


class _Settings:
    def __init__(self, **overrides):
        self.data = {
            'disable_llm_models': False,
            'disable_qa_corrections': False,
            'disable_knowledge_base_search': False,
            'disable_hybrid_search': False,
            'search_top_k': 5,
            'search_alpha': 0.7,
            'relevance_threshold': 0.2,
            'max_context_fragments': 100,
            'chat_memory_context_questions': 2,
            'review_scenario_enabled': True,
            'review_examples_count': 3,
            'llm_model': 'test-model',
            'llm_base_url': 'http://127.0.0.1:9/v1',
        }
        self.data.update(overrides)

    def reload(self):
        pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_llm_api_key(self):
        return 'test-key'


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {'choices': [{'message': {'content': 'ОТВЕТ МОДЕЛИ'}}]}


class _PostRecorder:
    def __init__(self):
        self.payloads = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json or {})
        return _FakeResponse()


def _stub_rag(user_id, **settings):
    rag = rag_core.RAGCore.__new__(rag_core.RAGCore)
    rag.settings = _Settings(**settings)
    rag.current_user_id = user_id
    rag._setup_client = lambda: None
    rag._sanitize_text = lambda text: text
    rag.price_calls, rag.qa_calls = [], []

    def price(q):
        rag.price_calls.append(q)
        return None

    def qa(q):
        rag.qa_calls.append(q)
        return None

    rag._try_price_answer = price
    rag._find_qa_correction = qa

    def find_relevant_info(query, top_k=None, alpha=None, alt_query=None):
        return 'КОНТЕКСТ БАЗЫ', ['doc.txt']

    rag.find_relevant_info = find_relevant_info
    return rag


def test_ask_model(user_id, folder):
    print('\n3) ask_model: блок первым в контексте, правило в промте')
    post = _PostRecorder()
    original = rag_core.requests.post
    rag_core.requests.post = post
    try:
        rag = _stub_rag(user_id)
        answer = rag.ask_model(TASK_REVIEW, user_prompt='Ты — ассистент компании.')

        check('ответ модели получен', answer.startswith('ОТВЕТ МОДЕЛИ'), answer[:60])
        system = post.payloads[-1]['messages'][0]['content']
        check('правило сценария в системном промте',
              'Сценарий «ответ на отзыв»' in system, system[-300:])
        check('блок сценария попал в контекст', 'СЦЕНАРИЙ: ОТВЕТ НА ОТЗЫВ' in system, system[:200])
        check('блок идёт раньше фрагментов БЗ',
              system.index('СЦЕНАРИЙ: ОТВЕТ НА ОТЗЫВ') < system.index('КОНТЕКСТ БАЗЫ'),
              system[-400:])
        check('прайс-путь не перехватывает задачу про отзыв', rag.price_calls == [], rag.price_calls)
        check('✏️-исправление не применяется к отзыву', rag.qa_calls == [], rag.qa_calls)
        check('файл с примерами попал в «Источники»',
              'Отзывы' in answer and 'Источники' in answer, answer[-300:])
        check('текущий вопрос последним сообщением',
              post.payloads[-1]['messages'][-1]['content'] == TASK_REVIEW,
              post.payloads[-1]['messages'][-1]['content'][:60])

        # обычный вопрос — сценарий выключен, прежние пути работают
        rag2 = _stub_rag(user_id)
        rag2.settings = _Settings(review_scenario_enabled=False)
        rag2.ask_model('Сколько стоит мембрана VONTRON-1812?', user_prompt='Ты — ассистент.')
        check('выключатель review_scenario_enabled работает',
              'СЦЕНАРИЙ: ОТВЕТ НА ОТЗЫВ' not in post.payloads[-1]['messages'][0]['content'],
              post.payloads[-1]['messages'][0]['content'][:120])
        check('ценовой путь снова спрашивается', 'мембрана' in ' '.join(rag2.price_calls),
              rag2.price_calls)
    finally:
        rag_core.requests.post = original


def test_defaults():
    print('\n4) Настройки по умолчанию')
    defaults = rag_core.RAGSettings().default_settings
    check('review_scenario_enabled есть в default_settings',
          'review_scenario_enabled' in defaults, list(defaults)[:6])
    check('по умолчанию сценарий включён', defaults.get('review_scenario_enabled') is True)
    check('review_examples_count по умолчанию 3', defaults.get('review_examples_count') == 3)


def main():
    user_id, folder = make_user_dir()
    try:
        test_detect()
        test_block(folder)
        test_ask_model(user_id, folder)
        test_defaults()
    finally:
        try:
            for fn in os.listdir(folder):
                os.remove(os.path.join(folder, fn))
            os.rmdir(folder)
        except OSError:
            pass

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
