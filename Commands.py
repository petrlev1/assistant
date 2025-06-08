import openai
import pyautogui
import webbrowser
import os
import subprocess
import pytesseract
import cv2
import numpy as np

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)

# Убедись, что Tesseract установлен и добавлен в PATH
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

def get_text_positions():
    # Делаем скриншот экрана
    img = pyautogui.screenshot()
    img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    # Распознаём текст
    data = pytesseract.image_to_data(img, lang='rus+eng', output_type=pytesseract.Output.DICT)

    # Сохраняем координаты каждого слова
    text_positions = []
    n_boxes = len(data['text'])
    for i in range(n_boxes):
        if int(data['conf'][i]) > 60:  # уверенность > 60%
            (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
            center = (x + w // 2, y + h // 2)
            text_positions.append({
                'text': data['text'][i].lower(),
                'box': (x, y, x + w, y + h),
                'center': center
            })
    return text_positions

def execute_dynamic_command(command):
    print(f"[Команда]: {command}")
    words = command.lower().split()

    # Получаем все найденные слова на экране
    text_positions = get_text_positions()

    # Примеры: "Нажми на поиск", "Кликни на параметры"
    for word in words:
        for item in text_positions:
            if word in item['text']:
                print(f"🔍 Нашёл '{item['text']}' на экране.")
                pyautogui.click(item['center'])
                return f"✅ Кликнул на '{item['text']}'."
    
    print("Элемент не найден на экране.")
    return "Элемент не найден."

def get_model_response(user_input):
    try:
        response = client.chat.completions.create(
            model="deepseek/deepseek-r1-0528-qwen3-8b",
            messages=[
                {"role": "system", "content": "Ты помощник, который переводит естественные команды пользователя в точные инструкции для компьютера. Отвечай коротко и точно."},
                {"role": "user", "content": user_input}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Ошибка при обращении к модели]: {e}")
        return ""
    
    print("🤖 Введите команду (или 'выход'):")
while True:
    user_input = input("Вы: ")
    if user_input.lower() in ["выход", "quit", "exit"]:
        print("👋 Завершаю работу.")
        break

    interpreted = get_model_response(user_input)
    print(f"🧠 [Модель]: {interpreted}")

    result = execute_dynamic_command(interpreted)
    print(f"💻 [Результат]: {result}")