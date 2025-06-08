import openai
from sentence_transformers import util, SentenceTransformer

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)


# === Твои данные ===
my_knowledge = [
    "Python — это язык программирования, разработанный Гвидо ван Россумом.",
    "OpenRouter — это сервис, предоставляющий доступ к различным LLM через единое API.",
    "GPT-4 — одна из самых мощных языковых моделей от OpenAI.",
    "LLaMA — открытая модель от Meta.",
    "Левендеев — программист дизайнер",
]

# === Настройка модели для поиска релевантного контента (локально) ===
model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True)

def find_relevant_info(query):
    query_embedding = model.encode(query, convert_to_tensor=True)
    hits = util.semantic_search(query_embedding, corpus_embeddings, top_k=1)
    return my_knowledge[hits[0][0]['corpus_id']]

# === Запрос к модели с поддержкой своего контекста ===
def ask_model(question):
    context = find_relevant_info(question)
    response = client.chat.completions.create(
        model="deepseek/deepseek-r1-0528-qwen3-8b",  # или любая другая модель
        messages=[
            {"role": "system", "content": f"Ответь на вопрос, используя следующую информацию:\n{context}"},
            {"role": "user", "content": question}
        ]
    )
    return response.choices[0].message.content.strip()

# === Пример использования ===
question = input("Ваш вопрос для поиска по базе знаний: ")
answer = ask_model(question)
print("Ответ:", answer)