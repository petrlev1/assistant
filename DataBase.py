import openai
import os
import csv
from sentence_transformers import util, SentenceTransformer
from rank_bm25 import BM25Okapi
import numpy as np
import pickle
import hashlib
import torch
from pathlib import Path
import PyPDF2

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "mistralai/mistral-7b-instruct:free"
]

def get_file_embedding_cache_path(file_path, knowledge_content):
    """Генерирует путь к кэш-файлу для конкретного файла знаний"""
    content_hash = hashlib.md5(str(sorted(knowledge_content)).encode('utf-8')).hexdigest()
    cache_dir = Path("embeddings_cache")
    cache_dir.mkdir(exist_ok=True)
    filename = Path(file_path).stem
    return cache_dir / f"{filename}_{content_hash[:12]}.pkl"

def load_or_create_embeddings(file_path, knowledge_content, model):
    """Загружает эмбеддинги из кэша или создает их заново"""
    cache_path = get_file_embedding_cache_path(file_path, knowledge_content)
    
    if cache_path.exists():
        print(f"💾 Загрузка эмбеддингов из кэша для {file_path}...")
        with open(cache_path, 'rb') as f:
            embeddings = pickle.load(f)
        print(f"✅ Эмбеддинги загружены из кэша для {file_path}.")
    else:
        print(f"🧠 Создание эмбеддингов для {file_path}...")
        embeddings = model.encode(knowledge_content, convert_to_tensor=True, show_progress_bar=True)
        with open(cache_path, 'wb') as f:
            pickle.dump(embeddings, f)
        print(f"✅ Эмбеддинги сохранены в кэш: {cache_path}")
    
    return embeddings

def load_knowledge_from_txt(file_path="DataBase.txt"):
    """Загрузка знаний из txt, CSV и PDF файлов в папке Database"""
    all_knowledge = {}
    
    # 1. Загрузка из txt
    txt_knowledge = []
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    txt_knowledge.append(line)
        print("📄 Содержимое DataBase.txt:")
        for i, line in enumerate(txt_knowledge, start=1):
            print(f"{i}. {line}")
        print(f"✅ Загружено {len(txt_knowledge)} строк из txt")
        all_knowledge[file_path] = txt_knowledge
    except FileNotFoundError:
        print(f"❌ Файл {file_path} не найден.")

    # 2. Загрузка всех файлов из папки Database
    database_folder = "Database"
    if not os.path.exists(database_folder):
        print(f"⚠️ Папка '{database_folder}' не найдена.")
    else:
        files = os.listdir(database_folder)
        csv_files = [f for f in files if f.lower().endswith(".csv")]
        pdf_files = [f for f in files if f.lower().endswith(".pdf")]
        
        # Обработка CSV файлов
        if not csv_files:
            print(f"⚠️ В папке '{database_folder}' нет CSV-файлов.")
        else:
            for csv_file in csv_files:
                file_path = os.path.join(database_folder, csv_file)
                csv_knowledge = []
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
                                if "Проект" in row and "Ответственный" in row:
                                    entry = f"Проект {row['Проект']} находится в статусе «{row.get('Статус', 'не указан')}». Ответственный — {row['Ответственный']}."
                                elif "Имя" in row and "Должность" in row:
                                    entry = f"{row['Имя']} работает {row['Должность']}. Контакт: email — {row.get('Email', 'не указан')}, телефон — {row.get('Телефон', 'не указан')}."
                                else:
                                    entry = "В компании Аквесегмент: " + ", ".join(parts) + "."
                                csv_knowledge.append(entry)
                    print(f"✅ Загружено {len(csv_knowledge)} строк из файла: {csv_file}")
                    all_knowledge[file_path] = csv_knowledge
                except Exception as e:
                    print(f"❌ Ошибка при чтении файла {csv_file}: {e}")

        # Обработка PDF файлов
        if not pdf_files:
            print(f"⚠️ В папке '{database_folder}' нет PDF-файлов.")
        else:
            for pdf_file in pdf_files:
                file_path = os.path.join(database_folder, pdf_file)
                pdf_knowledge = []
                try:
                    with open(file_path, 'rb') as f:
                        pdf_reader = PyPDF2.PdfReader(f)
                        print(f"📄 Обработка PDF файла: {pdf_file} (всего страниц: {len(pdf_reader.pages)})")
                        
                        for page_num, page in enumerate(pdf_reader.pages):
                            text = page.extract_text()
                            if text:
                                # Разбиваем текст на абзацы
                                paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
                                for paragraph in paragraphs:
                                    # Фильтруем короткие абзацы
                                    if len(paragraph) > 50:
                                        pdf_knowledge.append(f"[{pdf_file}, стр. {page_num+1}] {paragraph}")
                    
                    if pdf_knowledge:
                        print(f"✅ Извлечено {len(pdf_knowledge)} фрагментов из файла: {pdf_file}")
                        all_knowledge[file_path] = pdf_knowledge
                    else:
                        print(f"⚠️ Из файла {pdf_file} не удалось извлечь текст или он пуст.")
                        
                except Exception as e:
                    print(f"❌ Ошибка при чтении PDF файла {pdf_file}: {e}")

    if not all_knowledge:
        all_knowledge["default"] = [
            "База знаний пуста. Пожалуйста, создайте файл DataBase.txt или добавьте файлы в папку Database."
        ]

    return all_knowledge

# === Подготовка к гибридному поиску ===
all_knowledge_dict = load_knowledge_from_txt()

# Объединяем все знания в один список для поиска
my_knowledge = []
file_mapping = {}
current_index = 0

for file_path, knowledge_list in all_knowledge_dict.items():
    my_knowledge.extend(knowledge_list)
    for i in range(len(knowledge_list)):
        file_mapping[current_index + i] = file_path
    current_index += len(knowledge_list)

print(f"📊 Всего загружено {len(my_knowledge)} фрагментов знаний из {len(all_knowledge_dict)} файлов.")

# Модель для семантического поиска
model = SentenceTransformer('intfloat/multilingual-e5-large')

# Создаем/загружаем эмбеддинги для каждого файла отдельно
all_embeddings = []
for file_path, knowledge_list in all_knowledge_dict.items():
    file_embeddings = load_or_create_embeddings(file_path, knowledge_list, model)
    all_embeddings.append(file_embeddings)

# Объединяем все эмбеддинги в один тензор
if len(all_embeddings) > 1:
    corpus_embeddings = torch.cat(all_embeddings, dim=0)
else:
    corpus_embeddings = all_embeddings[0]

# Подготовка для BM25 (лексический поиск)
tokenized_corpus = [doc.split(" ") for doc in my_knowledge]
bm25 = BM25Okapi(tokenized_corpus)

def find_relevant_info(query, top_k=5, alpha=0.7):
    """Гибридный поиск: комбинирует семантический и лексический поиск."""
    global corpus_embeddings

    if not my_knowledge:
        print("⚠️ База знаний пуста.")
        return "Лебедев — это великий космонавт."

    if len(corpus_embeddings) != len(my_knowledge):
        all_embeddings = []
        for file_path, knowledge_list in all_knowledge_dict.items():
            file_embeddings = load_or_create_embeddings(file_path, knowledge_list, model)
            all_embeddings.append(file_embeddings)
        
        if len(all_embeddings) > 1:
            corpus_embeddings = torch.cat(all_embeddings, dim=0)
        else:
            corpus_embeddings = all_embeddings[0]

    # Семантический поиск
    query_embedding = model.encode(query, convert_to_tensor=True)
    semantic_hits = util.semantic_search(query_embedding, corpus_embeddings, top_k=len(my_knowledge))
    semantic_scores = {hit['corpus_id']: hit['score'] for hit in semantic_hits[0]}

    # Лексический поиск (BM25)
    tokenized_query = query.split(" ")
    bm25_scores = bm25.get_scores(tokenized_query)
    
    # Нормализация BM25 оценок
    if np.max(bm25_scores) > 0:
        bm25_scores_norm = bm25_scores / np.max(bm25_scores)
    else:
        bm25_scores_norm = bm25_scores

    # Комбинирование оценок
    combined_scores = {}
    for i in range(len(my_knowledge)):
        sem_score = semantic_scores.get(i, 0.0)
        bm25_score = bm25_scores_norm[i]
        combined_scores[i] = alpha * sem_score + (1 - alpha) * bm25_score

    # Сортировка по комбинированной оценке
    sorted_indices = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)
    
    # Выбор топ-K результатов
    top_indices = sorted_indices[:top_k]
    
    # Фильтрация по порогу
    best_score = combined_scores[top_indices[0]] if top_indices else 0
    if best_score < 0.2:
        print("⚠️ Низкая релевантность найденных данных.")
        return "Я не знаю ответа на этот вопрос."

    result = [my_knowledge[idx] for idx in top_indices]
    print(f"🔍 Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
    return "\n".join(result)

def ask_model(question, selected_model):
    context = find_relevant_info(question, top_k=5, alpha=0.7)
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
        answer = response.choices[0].message.content.strip()
        return f"\nОтвет ИИ модели:\n{answer}"
    except Exception as e:
        return f"Ответ ИИ модели:\nОшибка при обращении к модели: {e}"

# === Основной цикл ===
if __name__ == "__main__":
    print("Добро пожаловать в систему RAG с гибридным поиском!")
    print("Поддерживаются файлы: TXT, CSV, PDF")
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