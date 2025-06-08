import openai
import pyautogui
import webbrowser
import os
import subprocess

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1", 
    api_key="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"  # <-- замени на свой ключ
)

# === Функция для выполнения действий ===
def execute_command(command):
    command = command.lower()

    if "браузер" in command or "google chrome" in command:
        webbrowser.open("https://google.com") 
        return "Открываю браузер."

    elif "скриншот" in command:
        pyautogui.screenshot("screenshot.png")
        return "Сделал скриншот рабочего стола."

    elif "выключи компьютер" in command:
        os.system("shutdown /s /t 1")
        return "Выключаю компьютер."

    elif "блокнот" in command or "notepad" in command:
        subprocess.Popen("notepad.exe")
        return "Запускаю Блокнот."

    elif "закрой окно" in command:
        pyautogui.hotkey('alt', 'f4')
        return "Закрываю текущее окно."

    else:
        return "Не понял команду. Попробуйте ещё раз."
    
    # === Обращение к модели ===
def get_model_response(user_input):
    response = client.chat.completions.create(
        model="mistralai/mistral-7b-instruct",  # можно выбрать другую модель
        messages=[
            {"role": "system", "content": "Ты помощник, который переводит естественные команды пользователя в простые инструкции для управления компьютером."},
            {"role": "user", "content": user_input}
        ]
    )
    return response.choices[0].message.content.strip()

# === Основной цикл для ввода действия ===
print("Введите команду или 'выход' для завершения.")
while True:
    user_text = input("Какую команду выполнить?: ")
    if user_text.lower() in ["выход", "exit", "quit"]:
        print("Завершаю работу.")
        break

    # Получаем ответ модели
    try:
        interpreted_command = get_model_response(user_text)
        print(f"[Модель]: {interpreted_command}")

        # Выполняем команду
        result = execute_command(interpreted_command)
        print(f"[Система]: {result}")
    except Exception as e:
        print(f"[Ошибка]: {e}")