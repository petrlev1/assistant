import openai
from sentence_transformers import util, SentenceTransformer
import pandas as pd

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)



# === Работа с данными из Excel ===

# === Загрузка данных из Excel ===
df = pd.read_excel("../knowledge.xlsx")

# Проверяем наличие нужных столбцов
assert all(col in df.columns for col in ["Товар", "Вопрос", "Ответ"]), \
    "Файл должен содержать колонки: Товар, Вопрос, Ответ"

# Формируем полные вопросы и список ответов
questions = (df["Товар"] + ": " + df["Вопрос"]).tolist()
answers = df["Ответ"].tolist()

# === Настройка модели семантического поиска ===
model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
question_embeddings = model.encode(questions, convert_to_tensor=True)

# === Поиск наиболее подходящего ответа ===
def find_relevant_answer(user_query):
    query_embedding = model.encode(user_query, convert_to_tensor=True)
    hits = util.semantic_search(query_embedding, question_embeddings, top_k=1)
    best_match_index = hits[0][0]['corpus_id']
    return answers[best_match_index]

# === Запрос к модели ИИ с контекстом ===
def ask_model(question):
    context = find_relevant_answer(question)
    response = client.chat.completions.create(
        model="deepseek/deepseek-r1-0528-qwen3-8b",  # или любая другая модель
        messages=[
            {"role": "system", "content": f"Ответь на вопрос, используя следующую информацию:\n{context}"},
            {"role": "user", "content": question}
        ]
    )
    return response.choices[0].message.content.strip()

# === Пример использования ===
if __name__ == "__main__":
    user_question = input("Ваш вопрос из xls: ")
    answer = ask_model(user_question)
    print("Ответ:", answer)

# === /Работа с данными из Excel ===





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

# === /Твои данные ===