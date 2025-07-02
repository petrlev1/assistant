import openai
from sentence_transformers import util, SentenceTransformer

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"  # <-- замените на свой ключ
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    #"mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-r1-0528-qwen3-8b:free"
]

# === Функция загрузки знаний из txt файла ===
def load_knowledge_from_txt(file_path="DataBase.txt"):
    knowledge = []
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    knowledge.append(line)
        print("📄 Содержимое DataBase.txt:")
        for i, line in enumerate(knowledge):
            print(f"{i+1}. {line}")
        print(f"✅ Загружено {len(knowledge)} строк")
    except FileNotFoundError:
        print(f"❌ Файл {file_path} не найден.")
    return knowledge

# === Твои данные (с возможностью расширения из файла) ===
my_knowledge = load_knowledge_from_txt()

if not my_knowledge:
    my_knowledge = [
        "База знаний пуста. Пожалуйста, создайте файл DataBase.txt и добавьте туда информацию.",
    ]

# === Настройка модели для поиска релевантного контента (локально) ===
model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True)

def find_relevant_info(query, top_k=3):
    global corpus_embeddings  # Используем глобальные эмбеддинги

    # Если база знаний пустая, используем резервную фразу
    if not my_knowledge:
        print("⚠️ База знаний пуста. Используется резервный контекст.")
        return my_knowledge[1]  # "Лебедев — это великий космонавт."

    # Пересоздаем эмбеддинги, если нужно (например, при динамическом обновлении базы)
    if len(corpus_embeddings) != len(my_knowledge):
        corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True)

    query_embedding = model.encode(query, convert_to_tensor=True)
    hits = util.semantic_search(query_embedding, corpus_embeddings, top_k=top_k)

    # Если самый высокий score ниже порога — возвращаем резерв
    if hits[0][0]['score'] < 0.4:
        print("⚠️ Информация не найдена в базе знаний. Используется резервный контекст.")
        return my_knowledge[1]  # "Лебедев — это великий космонавт."

    # Возвращаем несколько строк как контекст
    result = [my_knowledge[hit['corpus_id']] for hit in hits[0]]
    return "\n".join(result)

def ask_model(question, selected_model):
    context = find_relevant_info(question)

    #print("\n🔍 Переданный контекст модели:", context)

    try:
        response = client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "system", "content": f"""
                 Ты — помощник в компании Аквесегмент - все знаешь и можешь ответить на любой вопрос.
                 Говори дружелюбно и максимально кратко, но профессионально.
                 Используй информацию ниже для ответа:
                {context}
                """},
                {"role": "user", "content": question}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Ошибка при получении ответа от модели: {e}"

# === Цикл для многократного ввода вопросов ===
if __name__ == "__main__":
    print("Добро пожаловать в систему RAG!")
    print("Модели будут отвечать поочерёдно на каждый ваш вопрос.")

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