from openai import OpenAI

# === Настройка клиента OpenRouter ===
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",    
    api_key="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"  # ⚠️ Замени на свой ключ
)

if not client.api_key:
    raise ValueError("API ключ не найден.")

# === Список моделей, которые будем использовать ===
model_list = [
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-r1-0528-qwen3-8b:free"
]

# === Функция для отправки запроса модели ===
def ask_model(prompt, model_name):
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=512,
            top_p=0.9,
            frequency_penalty=0.0,
            presence_penalty=0.0
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[Ошибка] {str(e)}"

# === Пример использования: опрос всех моделей ===
if __name__ == "__main__":
    question = "Проанализируй рассказы Чехова."
    print("Запрашиваем у моделей:\n")
    
    for model in model_list:
        print(f"🔹 Модель: {model}")
        answer = ask_model(question, model)
        print(f"📝 Ответ:\n{answer}")
        print("-" * 80 + "\n")