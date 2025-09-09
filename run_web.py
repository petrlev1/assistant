# run_web.py - Файл для запуска веб-интерфейса
"""Скрипт для запуска веб-интерфейса RAG-системы"""

print("🚀 Запуск веб-интерфейса RAG-системы...")

try:
    from web_app import create_app
    app = create_app()
    print("✅ Веб-интерфейс успешно инициализирован")
    print("🌐 Сервер доступен по адресу: http://localhost:5000")
    print("🛑 Для остановки сервера нажмите Ctrl+C")
    
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Убедитесь, что файл web_app.py находится в одной папке с этим файлом")
except Exception as e:
    print(f"❌ Ошибка запуска веб-интерфейса: {e}")
    import traceback
    traceback.print_exc()