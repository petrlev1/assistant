import dashscope
from dashscope import Generation

# Установите ваш API ключ
dashscope.api_key = 'sk-bb2885064dc340d292343ca7bc762966'

def call_qwen(prompt):
    try:
        response = Generation.call(
            model='qwen-max',
            prompt=prompt
        )
        if response is None:
            print("API returned None.")
            return None
        elif response.error:
            print(f"API error: {response.error}")
            return None
        return response.output.text
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

# Пример использования
if __name__ == '__main__':
    user_prompt = "Расскажи интересный факт о космосе."
    answer = call_qwen(user_prompt)
    if answer:
        print("Ответ Qwen:")
        print(answer)
    else:
        print("Не удалось получить ответ от Qwen.")