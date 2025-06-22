import os
import time
import random
import math
import cv2
import numpy as np
import pytesseract
from mss import mss
from datetime import datetime
from PIL import Image
import pyautogui  # Для управления мышью
import openai
from dotenv import load_dotenv

# Укажите путь к Tesseract OCR
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Загрузка переменных окружения
load_dotenv()

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)

if not client.api_key:
    raise ValueError("API ключ не найден.")

# === Функция для получения ответа от модели ===
def ask_model(prompt):
    model_list = [
        "mistralai/mistral-7b-instruct-v0.1",   # Бесплатная модель
        "google/gemma-7b-it",                  # Хорошая инструкционная модель
        "huggingfaceh4/zephyr-7b-beta",         # Подходит для чата
        "microsoft/phi-2"                       # Лёгкая, мощная модель
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

    print("❌ Не удалось получить ответ ни от одной доступной модели.")
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
    config = '--oem 3 --psm 6 -c tessedit_char_whitelist=АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    data = pytesseract.image_to_data(gray, lang='rus+eng', config=config, output_type=pytesseract.Output.DICT)
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

# === Поиск координат текста на экране ===
def find_text_position(target_text):
    """
    Находит координаты центра заданного текста на экране.
    Возвращает (x, y) или None, если текст не найден.
    """
    with mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)

    # Преобразуем в массив NumPy и BGR для OpenCV
    img_np = np.array(img)
    img_cv2 = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(img_cv2, cv2.COLOR_BGR2GRAY)

    # OCR с координатами
    data = pytesseract.image_to_data(gray, lang='rus+eng', output_type=pytesseract.Output.DICT)

    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if text and target_text.lower() in text.lower():
            x = data["left"][i] + data["width"][i] // 2
            y = data["top"][i] + data["height"][i] // 2
            print(f"✅ Найден текст '{text}' на позиции: ({x}, {y})")
            return x, y

    print(f"❌ Текст '{target_text}' не найден.")
    return None


# === Плавное движение мыши как человек ===
def human_move_to(x, y, duration=1.0, steps=30, jitter=2):
    """
    Плавное движение мыши к точке (x, y), имитирующее человека.
    
    :param x: Целевая координата X
    :param y: Целевая координата Y
    :param duration: Общее время движения (в секундах)
    :param steps: Количество шагов
    :param jitter: Максимальное отклонение (в пикселях)
    """
    current_x, current_y = pyautogui.position()
    dx = x - current_x
    dy = y - current_y

    step_duration = duration / steps

    # easeOutQuad — быстрее в начале, медленнее в конце
    for i in range(1, steps + 1):
        t = i / steps
        easing = t * (2 - t)

        move_x = current_x + dx * easing + random.uniform(-jitter, jitter)
        move_y = current_y + dy * easing + random.uniform(-jitter, jitter)

        pyautogui.moveTo(move_x, move_y)
        time.sleep(step_duration)


# === Клик по определённому тексту ===
def click_on_text(target_text):
    """
    Ищет указанный текст на экране и кликает по нему,
    имитируя поведение человека.
    """
    position = find_text_position(target_text)
    if position:
        x, y = position

        # Случайная пауза перед движением
        time.sleep(random.uniform(0.5, 1.2))

        # Плавное движение мыши
        human_move_to(x, y, duration=random.uniform(0.8, 1.5))

        # Случайная задержка перед кликом
        time.sleep(random.uniform(0.3, 0.7))

        # Совершение клика
        pyautogui.click()

        print(f"🖱️ Клик по тексту '{target_text}' выполнен (человеческий стиль).")
    else:
        print("⚠️ Клик не выполнен — текст не найден.")


# === Основное меню ===
def main():
    print("🎥 Программа запущена. Выберите режим:")
    print("1 — Наблюдение за экраном (реальное время)")
    print("2 — Однократный анализ экрана")
    print("3 — Кликнуть по определённому тексту")
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
    elif choice == "3":
        print("🔤 Введите текст, по которому нужно кликнуть:")
        text_to_click = input("> ").strip()
        click_on_text(text_to_click)
    else:
        print("👋 Работа завершена.")

if __name__ == "__main__":
    main()