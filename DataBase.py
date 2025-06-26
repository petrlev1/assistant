import openai
from sentence_transformers import util, SentenceTransformer
import os

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
    "«Pertsev Company» — поставщик. Производит, выпускает и поставляет инновационные системы, которые обеспечивают безопасность и чистоту воды как для бытовых нужд в квартирах и частных домах, так и для промышленных производств. Доступ в личный кабинет Перцева: Личный кабинет на сайте: https://partner.pertsev.vip/login   ; Логин: 7705358239; Пароль: 7705358239",
]

# === Настройка модели для поиска релевантного контента (локально) ===
model = SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
corpus_embeddings = model.encode(my_knowledge, convert_to_tensor=True)

def find_relevant_info(query):
    query_embedding = model.encode(query, convert_to_tensor=True)
    hits = util.semantic_search(query_embedding, corpus_embeddings, top_k=1)

    # Проверяем степень релевантности: если score ниже порога — не возвращаем результат
    if hits[0][0]['score'] < 0.5:
        return "Информация не найдена."
    return my_knowledge[hits[0][0]['corpus_id']]

def ask_model(question):
    context = find_relevant_info(question)

    if context == "Информация не найдена.":
        return context

    try:
        response = client.chat.completions.create(
            model="mistralai/mistral-7b-instruct:free",  # Рекомендуемая бесплатная модель
            messages=[
                {"role": "system", "content": f"Ответь на вопрос, используя следующую информацию:\n{context}"},
                {"role": "user", "content": question}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Ошибка при получении ответа от модели: {e}"

# === Цикл для многократного ввода вопросов ===
if __name__ == "__main__":
    print("Добро пожаловать в систему RAG! Введите свой вопрос или 'выход' для завершения.")
    
    while True:
        user_input = input("\nВаш вопрос по базе знаний: ").strip()
        
        if user_input.lower() in ['выход', 'exit', 'q', 'quit']:
            print("Завершение работы. До свидания!")
            break
        
        answer = ask_model(user_input)
        print("Ответ:", answer)