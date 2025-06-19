import openai
import pyautogui
import pytesseract
import cv2
import numpy as np
import webbrowser
import subprocess
import os
import time
import re
from dotenv import load_dotenv
import random

# Укажи путь к tesseract.exe (если не в PATH)
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Загрузка переменных окружения
load_dotenv()

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)

if not client.api_key:
    raise ValueError("API ключ не найден. Установите переменную окружения OPENAI_API_KEY")

# === Функция для получения ответа от модели ===
def ask_model(prompt):
    model_list = [
        "deepseek/deepseek-r1-0528-qwen3-8b",  # Модель с ограничениями доступа
        "mistralai/mistral-7b-instruct-v0.1"   # Публичная альтернатива
    ]

    for model in model_list:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.7
            )
            answer = response.choices[0].message.content.strip()
            clean_answer = answer.split("\n")[0].strip()  # Берём только первую строку
            print(f"[DEBUG] Ответ модели ({model}): {repr(clean_answer)}")
            if clean_answer:
                return clean_answer
        except Exception as e:
            print(f"[Ошибка при использовании модели {model}]: {e}")
            continue

    print("❌ Не удалось получить ответ ни от одной модели.")
    return ""

# === Плавное движение мыши как у человека ===
def human_like_move(x, y, duration=0.5):
    """Плавно двигает мышь к цели с небольшим дрожанием"""
    start_x, start_y = pyautogui.position()
    steps = 10
    for i in range(steps):
        dx = (x - start_x) * (i / steps) + random.uniform(-5, 5)
        dy = (y - start_y) * (i / steps) + random.uniform(-5, 5)
        pyautogui.moveTo(start_x + dx, start_y + dy, duration=duration / steps)
    pyautogui.moveTo(x, y)  # Точное позиционирование в конце

# === Поиск текста на экране и клик по нему ===
def click_on_text(target_text, threshold=0.7):
    print(f"🔍 Ищу текст '{target_text}' на экране...")

    screenshot = pyautogui.screenshot()
    img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    data = pytesseract.image_to_data(gray, lang='rus+eng', output_type=pytesseract.Output.DICT)

    found = False
    n_boxes = len(data['text'])
    for i in range(n_boxes):
        text = data['text'][i]
        if text.lower() == target_text.lower():
            (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
            center_x = x + w // 2
            center_y = y + h // 2

            print(f"🖱️ Найдено: '{text}' на координатах ({x}, {y}) размером {w}x{h}")

            # Случайный сдвиг для натурального клика
            jitter_x = random.randint(-5, 5)
            jitter_y = random.randint(-5, 5)
            final_x = center_x + jitter_x
            final_y = center_y + jitter_y

            # Плавное движение мыши
            move_duration = random.uniform(0.3, 0.7)
            print(f"🧭 Перемещаюсь к точке за {move_duration:.2f} секунд...")
            human_like_move(final_x, final_y, duration=move_duration)

            # Пауза, как будто пользователь "думает"
            time.sleep(random.uniform(0.5, 1.0))

            # Клик
            pyautogui.click()
            print(f"✅ Естественный клик выполнен по '{text}'")
            found = True
            break

    if not found:
        print(f"❌ Текст '{target_text}' не найден на экране.")
    return found

# === Парсер команд ===
def execute_command(command):
    if not command:
        print("❌ Пустая команда.")
        return False

    print(f"[Выполняется]: {command}")

    cmd = command.lower()

    # --- Случай: модель вернула отказ ---
    if re.search(r"(не могу|не поддерживается|неизвестно)", cmd):
        print("🚫 Модель не может выполнить эту команду")
        return True

    # --- Печать текста ---
    write_match = re.search(r'(?:напиши|введи|печатай|type|write|print)\s+"?([^"\n]+)"?', cmd)
    if write_match:
        text = write_match.group(1).strip()
        time.sleep(1)
        pyautogui.write(text)
        print(f"✅ Напечатано: {text}")
        return True

    # --- Горячие клавиши ---
    key_match = re.search(r'(?:нажми|press|hotkey|клавиши|нажатие)\s+((?:ctrl|alt|shift|win|enter|esc|space|tab|f\d|→|←|↑|↓|delete|backspace|home|end|page\s*up|page\s*down)(?:\s*\+\s*(?:ctrl|alt|shift|win|enter|esc|space|tab|f\d|→|←|↑|↓|delete|backspace|home|end|page\s*up|page\s*down))*)', cmd, re.IGNORECASE)
    if key_match:
        keys_raw = key_match.group(1).lower()
        keys = re.split(r'\s*\+\s*', keys_raw)

        key_map = {
            "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
            "win": "winleft", "windows": "winleft",
            "alt": "alt", "option": "alt",
            "shift": "shift", "enter": "enter", "esc": "esc",
            "space": "space", "tab": "tab", "delete": "delete",
            "backspace": "backspace", "home": "home", "end": "end",
            "page up": "pageup", "page down": "pagedown"
        }

        mapped_keys = [key_map.get(k.strip(), k.strip()) for k in keys]
        pyautogui.hotkey(*mapped_keys)
        print(f"✅ Нажаты клавиши: {mapped_keys}")
        return True

    # --- Кликнуть по тексту ---
    click_match = re.search(r'(?:кликни|нажми|жми|click|press)\s+(?:на|по)?\s*(?:текст|кнопку|элемент|button|element)?\s*"?([^"\n]+)"?', cmd)
    if click_match:
        target_text = click_match.group(1).strip()
        result = click_on_text(target_text)
        return result

    # --- Открытие программы или сайта ---
    app_match = re.search(r'(?:открой|open|launch|run|start)\s+(.+)', cmd)
    if app_match:
        target = app_match.group(1).strip().lower()

        # Поддержка специальных случаев
        if "chrome" in target or "браузер" in target or "google" in target:
            target = "chrome"
        elif "notepad" in target or "блокнот" in target:
            target = "notepad"
        elif "проводник" in target or "explorer" in target:
            target = "explorer"
        elif "photoshop" in target or "фотошоп" in target:
            target = "photoshop.exe"

        if "." in target and not " " in target:
            webbrowser.open(target)
            print(f"🌐 Открыт сайт: {target}")
        else:
            try:
                if os.name == "nt":
                    os.startfile(target)
                else:
                    subprocess.run(["xdg-open", target])
                print(f"🟢 Запущено: {target}")
            except Exception as e:
                print(f"❌ Не удалось запустить '{target}': {e}")
        return True

    # --- Закрыть окно ---
    if re.search(r'(?:закрой окно|close window|exit|quit)', cmd):
        pyautogui.hotkey('alt', 'f4')
        print("🛑 Окно закрыто")
        return True

    print("❓ Команда не распознана или не поддерживается.")
    return False

# === Бесконечный цикл ввода команд ===
def main():
    print("🤖 Введите вашу команду для управления компьютером (или 'выход'):")

    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ["выход", "exit", "quit"]:
            print("👋 Работа завершена.")
            break

        system_prompt = f"""
Вы — голосовой помощник, который должен преобразовать естественный язык пользователя в конкретное действие.
ВАЖНО: Отвечайте ТОЛЬКО одной строкой на РУССКОМ языке.

Допустимые действия:
- Напиши [текст]
- Нажми [клавиши]
- Кликни по [текст]
- Открой [программа или сайт]
- Закрой окно

Примеры:
Пользователь: "Скажи привет"
Вы: Напиши привет

Пользователь: "{user_input}"
Вы:
"""

        attempt = 0
        success = False
        while attempt < 3 and not success:
            ai_response = ask_model(system_prompt)
            success = execute_command(ai_response)
            if not success:
                print("🔄 Модель вернула некорректную команду. Повторный запрос...")
                attempt += 1

        if not success:
            print("⚠️ Не удалось обработать команду после нескольких попыток.")

if __name__ == "__main__":
    main()