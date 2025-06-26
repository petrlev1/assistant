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
import webbrowser
import subprocess
import json

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
        "mistralai/mistral-7b-instruct:free",   # Бесплатная модель
        "google/gemma-7b-it:free",             # Хорошая инструкционная модель
        "huggingfaceh4/zephyr-7b-beta:free",    # Подходит для чата
        "microsoft/phi-2:free"                 # Лёгкая, мощная модель
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


# === Генерация унифицированного промта ===
def generate_prompt(current_text, previous_text=None):
    """
    Генерирует унифицированный промт для модели.
    
    :param current_text: Текст с текущего скриншота
    :param previous_text: Текст с предыдущего скриншота (если есть)
    :return: строка — готовый prompt
    """
    prompt = """You are the assistant. Describe what is shown on the screen.
Write in Russian.\n"""

    if previous_text:
        prompt += f"""
Текст до: "{previous_text[:500]}"
Текст сейчас: "{current_text[:500]}"\n
Описание изменений:
"""
    else:
        prompt += f"""
Найденный текст: "{current_text[:500]}"...\n
Описание:
"""

    return prompt


# === Сохранение скриншота в файл ===
def save_screenshot(img_array, prefix="screenshot"):
    """
    Сохраняет изображение с временной меткой
    :param img_array: массив NumPy с изображением
    :param prefix: префикс имени файла
    :return: путь к файлу
    """
    save_dir = "saved_screenshots"
    os.makedirs(save_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{timestamp}.png"
    filepath = os.path.join(save_dir, filename)

    Image.fromarray(img_array).save(filepath)
    print(f"🖼️ Скриншот сохранён: {filepath}")
    return filepath


# === Получает весь текст с экрана через mss + возвращает изображение ===
def get_screen_text_and_image():
    with mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)

    img_np = np.array(img)
    img_cv2 = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(img_cv2, cv2.COLOR_BGR2GRAY)

    save_screenshot(img_cv2, prefix="screen_raw")

    config = '--oem 3 --psm 6 -c tessedit_char_whitelist=АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    data = pytesseract.image_to_data(gray, lang='rus+eng', config=config, output_type=pytesseract.Output.DICT)
    recognized_text = " ".join([text for text in data['text'] if text.strip()])
    return recognized_text, img_cv2


# === Описание того, что видит модель ===
def describe_screen_changes(current_text, previous_text):
    prompt = generate_prompt(current_text, previous_text)
    return ask_model(prompt)


# === Режим постоянного наблюдения за экраном ===
def monitor_screen(interval=5):
    print(f"👁️ Начинаю режим наблюдения за экраном (интервал: {interval} секунд). Ctrl+C — остановить.")

    last_text = ""
    last_image = None

    try:
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
    except KeyboardInterrupt:
        print("\n👋 Работа в режиме наблюдения завершена.")


# === Поиск координат текста на экране ===
def find_text_position(target_text):
    with mss() as sct:
        monitor = sct.monitors[1]
        img = sct.grab(monitor)

    img_np = np.array(img)
    img_cv2 = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
    gray = cv2.cvtColor(img_cv2, cv2.COLOR_BGR2GRAY)

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
    current_x, current_y = pyautogui.position()
    dx = x - current_x
    dy = y - current_y
    step_duration = duration / steps

    for i in range(1, steps + 1):
        t = i / steps
        easing = t * (2 - t)  # easeOutQuad

        move_x = current_x + dx * easing + random.uniform(-jitter, jitter)
        move_y = current_y + dy * easing + random.uniform(-jitter, jitter)

        pyautogui.moveTo(move_x, move_y)
        time.sleep(step_duration)


# === Клик по определённому тексту с задержкой ===
def click_on_text(target_text):
    position = find_text_position(target_text)
    if position:
        x, y = position

        time.sleep(random.uniform(0.5, 1.2))
        human_move_to(x, y, duration=random.uniform(0.8, 1.5))
        pyautogui.moveRel(random.randint(-5, 5), random.randint(-5, 5))
        time.sleep(random.uniform(0.2, 0.5))
        pyautogui.click()

        print(f"🖱️ Клик по тексту '{target_text}' выполнен (человеческий стиль).")
    else:
        print("⚠️ Клик не выполнен — текст не найден.")


# === Парсинг команды от пользователя через модель ===
def parse_multi_action_command(command):
    prompt = f"""
Проанализируй команду пользователя и верни список действий в формате JSON.
Поддерживаемые действия: open_browser, click_text, type_text, launch_app.

Команда: "{command}"

Формат:
[
  {{"action": "open_browser", "target": "https://example.com"}}, 
  {{"action": "click_text", "target": "Кнопка Войти"}}
]

Твоя задача — только вернуть JSON, без лишнего текста!
"""

    response = ask_model(prompt)

    try:
        actions = json.loads(response)
        return actions
    except json.JSONDecodeError as e:
        print(f"[Ошибка парсинга JSON]: {e}")
        print("[Попытка восстановления...]")
        try:
            fixed = response.replace("\n", "").replace(",}", "}").replace(":\"", ":\"").replace("\"}", "\"}")
            actions = json.loads(fixed)
            print(f"[Исправленный JSON]: {fixed}")
            return actions
        except Exception as inner_e:
            print(f"[Не удалось восстановить JSON]: {inner_e}")
            return []


# === Выполнение действия на основе JSON ===
def execute_action(action_data):
    if not action_data:
        print("❌ Не удалось распознать действие.")
        return

    action = action_data.get("action")
    target = action_data.get("target")

    if action == "open_browser" and target:
        if not target.startswith("http"):
            target = "https://"  + target
        print(f"🌐 Открываю браузер и перехожу на: {target}")
        webbrowser.open(target)
        time.sleep(random.uniform(3, 5))  # Ждём загрузки страницы

    elif action == "launch_app" and target:
        print(f"🚀 Запускаю приложение: {target}")
        try:
            if os.name == 'nt':
                os.startfile(target)
            else:
                subprocess.Popen([target])
        except Exception as e:
            print(f"❌ Ошибка при запуске приложения: {e}")

    elif (action == "click_text" or action == "click_element") and target:
        print(f"🖱️ Кликаю по элементу с текстом: '{target}'")
        click_on_text(target)

    elif action == "type_text" and target:
        print(f"⌨️ Печатаю текст: '{target}'")
        pyautogui.write(target, interval=0.15)

    else:
        print(f"❓ Неизвестное действие: {action}")


# === Выполнение цепочки действий ===
def execute_multi_action(actions):
    if not actions:
        print("❌ Нет действий для выполнения.")
        return

    print("🛠️ Выполняю последовательность действий:")
    for idx, action in enumerate(actions, start=1):
        print(f"{idx}. {action}")
        execute_action(action)


# === Основная функция для вызова из меню ===
def execute_natural_language_command(command):
    print("🧠 Анализирую команду...")
    parsed_actions = parse_multi_action_command(command)

    if parsed_actions:
        execute_multi_action(parsed_actions)
    else:
        print("❌ Не удалось понять команду.")


# === Основное меню ===
def main():
    print("🎥 Программа запущена. Выберите режим:")
    print("1 — Наблюдение за экраном (реальное время)")
    print("2 — Однократный анализ экрана")
    print("3 — Кликнуть по определённому тексту")
    print("4 — Ввести команду на естественном языке")
    choice = input("> ").strip()

    if choice == "1":
        monitor_screen()
    elif choice == "2":
        current_text, current_image = get_screen_text_and_image()
        save_screenshot(current_image, prefix="single_analysis")

        prompt = generate_prompt(current_text)
        description = ask_model(prompt)
        print("👁️ Ответ модели:", description)
    elif choice == "3":
        print("🔤 Введите текст, по которому нужно кликнуть:")
        text_to_click = input("> ").strip()
        click_on_text(text_to_click)
    elif choice == "4":
        print("🗣️ Введите команду на естественном языке:")
        user_command = input("> ").strip()
        execute_natural_language_command(user_command)
    else:
        print("👋 Работа завершена.")


if __name__ == "__main__":
    main()