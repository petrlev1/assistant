import pyautogui
import time
import webbrowser

#Действия пользователя
def open_browser_and_go_to_yandex():
    
    print("🖱️ Имитирую открытие браузера...")

    '''
    # 1. Нажимаем Win + 1 / 2 / 3 — чтобы открыть браузер с панели задач (пример для Chrome)
    pyautogui.hotkey('win', '1')  # замени на нужный номер ярлыка
    time.sleep(1)
    '''

    print("🌐 Открываю Яндекс в браузере по умолчанию...")
    webbrowser.open("yandex.ru") 
    
    time.sleep(0.5)
    # 2. Нажимаем Ctrl+L — выделяем адресную строку
    pyautogui.hotkey('ctrl', 'l')
    time.sleep(0.5)

    # 3. Печатаем адрес сайта
    pyautogui.write("https://mail.ru",  interval=0.1)
    time.sleep(0.5)

    pyautogui.press('enter')
    time.sleep(2)

    pyautogui.write("qqq",  interval=0.2)
    time.sleep(0.5)

    # 4. Нажимаем Enter
    pyautogui.press('enter')

    print("✅ Браузер открыт, переход на Яндекс выполнен.")

# Запуск
if __name__ == "__main__":
    open_browser_and_go_to_yandex()