# run_web.py - Файл для запуска веб-интерфейса
"""Скрипт для запуска веб-интерфейса RAG-системы"""

import socket

# Функция для определения IP-адреса
def get_local_ip():
    """Получить локальный IP-адрес"""
    try:
        # Подключаемся к внешнему адресу, чтобы узнать наш IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"

print("🚀 Запуск веб-интерфейса RAG-системы...")

try:
    from web_app import create_app
    app = create_app()
    print("✅ Веб-интерфейс успешно инициализирован")
    
    # Настройки хоста и порта
    HOST = '0.0.0.0'  # Слушаем на всех интерфейсах
    PORT = 8000       # Измените на нужный порт
    
    # Определяем IP для вывода в сообщении
    local_ip = get_local_ip()
    
    print(f"🌐 Сервер доступен по адресам:")
    print(f"   - Локально: http://localhost:{PORT}")
    print(f"   - В сети: http://{local_ip}:{PORT}")
    print(f"   - Из интернета: http://ваш-внешний-ip:{PORT}")
    print("🛑 Для остановки сервера нажмите Ctrl+C")
    print("-" * 50)
    
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Убедитесь, что файл web_app.py находится в одной папке с этим файлом")
except Exception as e:
    print(f"❌ Ошибка запуска веб-интерфейса: {e}")
    import traceback
    traceback.print_exc()