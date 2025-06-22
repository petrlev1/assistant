import os
import time
import cv2
import numpy as np
import pytesseract
from mss import mss
from datetime import datetime
from PIL import Image
import openai
from dotenv import load_dotenv

# Укажите путь к Tesseract OCR
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Загрузка переменных окружения
load_dotenv()

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key=os.getenv("OPENROUTER_API_KEY")
)

if not client.api_key:
    raise ValueError("API ключ не найден.")

# === Функция для получения ответа от модели ===
def ask_model(prompt):
    model_list = [
        "mistralai/mistral-7b-instruct-v0.1",
        "google/gemma-7b-it",
        "huggingfaceh4/zephyr-7b-beta",
        "microsoft/phi-2"
    ]

    for model in model_list:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.7
            )
            answer = response.choices[0].message.content.strip()
            print(f"[DEBUG] Ответ модели ({model}): {repr(answer)}")
            return answer
        except Exception as e:
            print(f"[Ошибка при использовании модели {model}]: {e}")
            continue

    print("❌ Не удалось получить ответ ни от одной модели.")
    return ""

# === Сохранение скриншота в файл ===
def save_screenshot(img_array, prefix="screenshot"):
    """
    Сохраняет изображение с временной меткой
    :param img_array: массив NumPy с изображением
    :param prefix: префикс имени файла
    :return: путь к файлу
    """
    # Создаём папку для скриншотов, если её нет
    save_dir = "saved_screenshots"
    os.makedirs(save_dir, exist_ok=True)

    # Формируем имя файла с текущей датой и временем
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{timestamp}.png"
    filepath = os.path.join(save_dir, filename)

    # Сохраняем изображение
    Image.fromarray(img_array).save(filepath)
    print(f"🖼️ Скриншот сохранён: {filepath}")
    return filepath

# === Получает весь текст с экрана через mss + возвращает изображение ===
def get_screen_text_and_image():
    with mss() as sct:
        monitor = sct.monitors[1]  # Первый монитор
        img = sct.grab(monitor)

    # Преобразуем в массив NumPy и BGR для OpenCV
    img_np = np.array(img)
    img_cv2 = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(img_cv2, cv2.COLOR_BGR2GRAY)

    # Сохраняем оригинальное изображение
    save_screenshot(img_cv2, prefix="screen_raw")

    # OCR
    data = pytesseract.image_to_data(gray, lang='rus+eng', output_type=pytesseract.Output.DICT)
    recognized_text = " ".join([text for text in data['text'] if text.strip()])
    return recognized_text, img_cv2  # Возвращаем текст и изображение

# === Описание того, что видит модель ===
def describe_screen_changes(current_text, previous_text):
    prompt = f"""
Вы — голосовой помощник. Объясните, что изменилось на экране.
Пиши на русском языке.
Текст до: "{previous_text[:500]}"
Текст сейчас: "{current_text[:500]}"

Описание изменений:
"""

    return ask_model(prompt)

# === Режим постоянного наблюдения за экраном ===
def monitor_screen(interval=5):
    print(f"👁️ Начинаю режим наблюдения за экраном (интервал: {interval} секунд). Ctrl+C — остановить.")

    last_text = ""
    last_image = None

    while True:
        current_text, current_image = get_screen_text_and_image()

        if not current_text:
            print("[!] На экране не найдено текста.")
        elif current_text != last_text:
            print("\n🔄 Обнаружены изменения на экране...")
            description = describe_screen_changes(current_text, last_text)

            # Сохраняем изображение с пометкой "изменения"
            save_screenshot(current_image, prefix="change_detected")

            print("🧠 Описание изменений:", description)
            last_text = current_text
            last_image = current_image
        else:
            print("✔️ Экран не изменился.")

        time.sleep(interval)

# === Основное меню ===
def main():
    print("🎥 Программа запущена. Выберите режим:")
    print("1 — Наблюдение за экраном (реальное время)")
    print("2 — Однократный анализ экрана")
    choice = input("> ").strip()

    if choice == "1":
        monitor_screen()
    elif choice == "2":
        current_text, current_image = get_screen_text_and_image()
        save_screenshot(current_image, prefix="single_analysis")

        prompt = f"""
Вы — голосовой помощник. Опишите кратко, что вы видите на экране.
Пиши на русском языке.
Найденный текст: "{current_text[:500]}"...

Описание:
"""
        description = ask_model(prompt)
        print("👁️ Ответ модели:", description)
    else:
        print("👋 Работа завершена.")

if __name__ == "__main__":
    main()