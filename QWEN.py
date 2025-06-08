import dashscope
from dashscope import Generation

# Установите ваш API ключ
dashscope.api_key = 'sk-bb2885064dc340d292343ca7bc762966'

def call_qwen(prompt):
    response = Generation.call(
        model='qwen-max',  # Можно использовать qwen-plus, qwen-turbo и т.д.
        prompt=prompt
    )
    return response.output.text

# Пример использования
if __name__ == '__main__':
    user_prompt = "Расскажи интересный факт о космосе."
    answer = call_qwen(user_prompt)
    print("Ответ Qwen:")
    print(answer)