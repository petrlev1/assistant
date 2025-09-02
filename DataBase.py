import openai
import os
import csv
from sentence_transformers import util, SentenceTransformer
from rank_bm25 import BM25Okapi
import numpy as np

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"  # Замените на свой ключ
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "mistralai/mistral-7b-instruct:free"
    #"deepseek/deepseek-r1-0528-qwen3-8b:free"
]

# === Загрузка знаний из txt и всех CSV в папке Database ===
def load_knowledge_from_txt(file_path="DataBase.txt"):
    knowledge = []

    # 1. Загрузка из txt
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    knowledge.append(line)
        print("📄 Содержимое DataBase.txt:")
        for i, line in enumerate(knowledge, start=1):
            print(f"{i}. {line}")
        print(f"✅ Загружено {len(knowledge)} строк из txt")
    except FileNotFoundError:
        print(f"❌ Файл {file_path} не найден.")

    # 2. Загрузка всех CSV из папки Database
    database_folder = "Database"
    if not os.path.exists(database_folder):
        print(f"⚠️ Папка '{database_folder}' не найдена.")
    else:
        csv_files = [f for f in os.listdir(database_folder) if f.lower().endswith(".csv")]
        if not csv_files:
            print(f"⚠️ В папке '{database_folder}' нет CSV-файлов.")
        else:
            for csv_file in csv_files:
                file_path = os.path.join(database_folder, csv_file)
                try:
                    with open(file_path, "r", encoding="utf-8") as csvfile:
                        reader = csv.DictReader(csvfile)
                        rows = list(reader)
                        if not rows:
                            print(f"📌 Файл {csv_file} пуст.")
                            continue

                        for row in rows:
                            parts = []
                            for key, value in row.items():
                                clean_key = key.strip()
                                if clean_key.lower().startswith("unnamed"):
                                    continue
                                if value and value.strip():
                                    parts.append(f"{clean_key} — {value.strip()}")

                            if parts:
                                # Улучшенный формат: делаем из данных читаемые предложения
                                if "Проект" in row and "Ответственный" in row:
                                    entry = f"Проект {row['Проект']} находится в статусе «{row.get('Статус', 'не указан')}». Ответственный — {row['Ответственный']}."
                                elif "Имя" in row and "Должность" in row:
                                    entry = f"{row['Имя']} работает {row['Должность']}. Контакт: email — {row.get('Email', 'не указан')}, телефон — {row.get('Телефон', 'не указан')}."
                                else:
                                    entry = "В компании Аквесегмент: " + ", ".join(parts) + "."
                                knowledge.append(entry)
                    print(f"✅ Загружено {len(rows)} строк из файла: {csv_file}")
                except Exception as e:
                    print(f"❌ Ошибка при чтении файла {csv_file}: {e}")

    if not knowledge:
        knowledge = [
            "База знаний пуста. Пожалуйста, создайте файл DataBase.txt или добавьте CSV-файлы в папку Database."
        ]

    return knowledge

# === Загрузка знаний ===
my_knowledge = load_knowledge_from_txt()

# === Подготовка к гибридному поиску ===
# 1. Модель для семантического поиска
# Используем более современную модель
model = SentenceTransformer('intfloat/multilingual-e5-large') # или 'BAAI/bge-m3'
corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True, show_progress_bar=True)

# 2. Подготовка для BM25 (лексический поиск)
# Токенизируем корпус для BM25
tokenized_corpus = [doc.split(" ") for doc in my_knowledge]
bm25 = BM25Okapi(tokenized_corpus)

def find_relevant_info(query, top_k=5, alpha=0.7): # Настройка гибридного поиска
    """
    Гибридный поиск: комбинирует семантический и лексический поиск.
    
    :param query: Вопрос пользователя
    :param top_k: Количество топ результатов
    :param alpha: Вес семантического поиска (0 - только BM25, 1 - только семантика)
    :return: Строка с релевантной информацией
    """
    global corpus_embeddings

    if not my_knowledge:
        print("⚠️ База знаний пуста.")
        return "Лебедев — это великий космонавт."

    # Проверка размера эмбеддингов
    if len(corpus_embeddings) != len(my_knowledge):
        corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True)

    # 1. Семантический поиск
    query_embedding = model.encode(query, convert_to_tensor=True)
    semantic_hits = util.semantic_search(query_embedding, corpus_embeddings, top_k=len(my_knowledge))
    semantic_scores = {hit['corpus_id']: hit['score'] for hit in semantic_hits[0]}

    # 2. Лексический поиск (BM25)
    tokenized_query = query.split(" ")
    bm25_scores = bm25.get_scores(tokenized_query)
    
    # Нормализация BM25 оценок
    if np.max(bm25_scores) > 0:
        bm25_scores_norm = bm25_scores / np.max(bm25_scores)
    else:
        bm25_scores_norm = bm25_scores

    # 3. Комбинирование оценок
    combined_scores = {}
    for i in range(len(my_knowledge)):
        sem_score = semantic_scores.get(i, 0.0)
        bm25_score = bm25_scores_norm[i]
        # Линейная комбинация
        combined_scores[i] = alpha * sem_score + (1 - alpha) * bm25_score

    # 4. Сортировка по комбинированной оценке
    sorted_indices = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)
    
    # 5. Выбор топ-K результатов
    top_indices = sorted_indices[:top_k]
    
    # Фильтрация по порогу (можно настроить)
    # Для гибридного подхода порог можно сделать более гибким
    # Например, если лучший результат ниже порога, вернуть "не знаю"
    best_score = combined_scores[top_indices[0]] if top_indices else 0
    if best_score < 0.2: # Порог можно экспериментально подобрать
        print("⚠️ Низкая релевантность найденных данных.")
        return "Я не знаю ответа на этот вопрос."

    result = [my_knowledge[idx] for idx in top_indices]
    print(f"🔍 Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
    return "\n".join(result)

def ask_model(question, selected_model):
    context = find_relevant_info(question)
    print("\n🔍 Переданный контекст модели:\n", context)

    try:
        response = client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "system", "content": f"""
Ты — профессиональный помощник компании Аквесегмент. Отвечай кратко, ясно и строго на основе информации ниже.

Правила:
- Если ответа нет в информации — скажи «Я не знаю».
- Не выдумывай и не предполагай.
- Анализируй данные и формулируй ответ самостоятельно.

Информация:
{context}
                """},
                {"role": "user", "content": question}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Ошибка при обращении к модели: {e}"

# === Основной цикл ===
if __name__ == "__main__":
    print("Добро пожаловать в систему RAG с гибридным поиском!")
    print("Модели будут отвечать поочерёдно на каждый ваш вопрос.")
    print("Введите 'выход', 'exit', 'q' или 'quit' для завершения.")

    while True:
        user_input = input("\nВаш вопрос по базе знаний: ").strip()

        if user_input.lower() in ['выход', 'exit', 'q', 'quit']:
            print("Завершение работы. До свидания!")
            break

        print(f"\n❓ Запрос: \"{user_input}\"")

        for model_name in AVAILABLE_MODELS:
            print(f"\n🤖 Ответ от модели {model_name}:")
            answer = ask_model(user_input, model_name)
            print(answer)