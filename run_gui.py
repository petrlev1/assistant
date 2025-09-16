# run_gui.py - Запуск графического интерфейса
from rag_core import get_rag_system, RAGSettings
from rag_gui import SettingsWindow

def main():
    """Запуск графического интерфейса настроек"""
    # Создание и отображение окна настроек
    rag = get_rag_system()
    settings = RAGSettings()
    settings_window = SettingsWindow(settings, rag)
    settings_window.show()

if __name__ == "__main__":
    main()