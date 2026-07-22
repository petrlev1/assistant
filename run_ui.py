# run_ui.py - Стартовая панель для запуска компонентов RAG-системы
"""Стартовая панель с кнопками запуска веб-интерфейса и GUI настроек"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import sys
import os
import webbrowser
import threading

class LauncherUI:
    def __init__(self, root):
        self.root = root
        self.root.title("RAG-система - Стартовая панель")
        self.root.geometry("400x350")
        self.root.resizable(False, False)
        
        # Путь к директории проекта
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Хранилище запущенных процессов
        self.web_process = None
        self.gui_process = None
        
        self.setup_ui()
        
    def setup_ui(self):
        """Настройка интерфейса"""
        # Заголовок
        title_label = tk.Label(
            self.root, 
            text="🚀 RAG-система", 
            font=("Arial", 18, "bold")
        )
        title_label.pack(pady=15)
        
        subtitle_label = tk.Label(
            self.root,
            text="Выберите компонент для запуска",
            font=("Arial", 10)
        )
        subtitle_label.pack(pady=5)
        
        # Разделитель
        separator = tk.Frame(self.root, height=2, bd=1, relief=tk.SUNKEN)
        separator.pack(fill=tk.X, padx=20, pady=15)
        
        # Кнопка запуска веб-интерфейса
        self.web_button = tk.Button(
            self.root,
            text="🌐 Веб-интерфейс",
            font=("Arial", 12),
            command=self.launch_web,
            width=25,
            height=2,
            bg="#4CAF50",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.web_button.pack(pady=8)
        
        # Кнопка запуска GUI настроек
        self.gui_button = tk.Button(
            self.root,
            text="⚙️ Настройки (GUI)",
            font=("Arial", 12),
            command=self.launch_gui,
            width=25,
            height=2,
            bg="#2196F3",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.gui_button.pack(pady=8)
        
        # Кнопка остановки всех процессов
        self.stop_button = tk.Button(
            self.root,
            text="🛑 Остановить всё",
            font=("Arial", 12),
            command=self.stop_all,
            width=25,
            height=2,
            bg="#f44336",
            fg="white",
            relief=tk.RAISED,
            cursor="hand2"
        )
        self.stop_button.pack(pady=8)
        
        # Статус бар
        self.status_label = tk.Label(
            self.root,
            text="Готов к запуску",
            font=("Arial", 9),
            fg="gray"
        )
        self.status_label.pack(side=tk.BOTTOM, pady=10)
        
    def launch_web(self):
        """Запуск веб-интерфейса"""
        if self.web_process and self.web_process.poll() is None:
            messagebox.showinfo(
                "Информация",
                "Веб-интерфейс уже запущен!"
            )
            return
            
        try:
            self.status_label.config(text="Запуск веб-интерфейса...", fg="green")
            
            # Запуск run_web.py как отдельного процесса
            python_exe = sys.executable
            script_path = os.path.join(self.project_dir, "run_web.py")
            
            self.web_process = subprocess.Popen(
                [python_exe, script_path],
                cwd=self.project_dir
            )
            
            self.status_label.config(text="✅ Веб-интерфейс запущен (порт 8077)", fg="green")
            
            # Автоматическое открытие браузера через задержку (чтобы сервер успел стартовать)
            def open_browser():
                import time
                time.sleep(2)
                webbrowser.open(f"http://localhost:8077")
            
            threading.Thread(target=open_browser, daemon=True).start()
            
        except Exception as e:
            messagebox.showerror(
                "Ошибка",
                f"Не удалось запустить веб-интерфейс:\n{str(e)}"
            )
            self.status_label.config(text="❌ Ошибка запуска", fg="red")
            
    def launch_gui(self):
        """Запуск GUI настроек"""
        if self.gui_process and self.gui_process.poll() is None:
            messagebox.showinfo(
                "Информация",
                "GUI настроек уже запущен!"
            )
            return
            
        try:
            self.status_label.config(text="Запуск GUI настроек...", fg="blue")
            
            # Запуск run_gui.py как отдельного процесса
            python_exe = sys.executable
            script_path = os.path.join(self.project_dir, "run_gui.py")
            
            self.gui_process = subprocess.Popen(
                [python_exe, script_path],
                cwd=self.project_dir
            )
            
            self.status_label.config(text="✅ GUI настроек запущен", fg="blue")
            
        except Exception as e:
            messagebox.showerror(
                "Ошибка",
                f"Не удалось запустить GUI настроек:\n{str(e)}"
            )
            self.status_label.config(text="❌ Ошибка запуска", fg="red")
            
    def stop_all(self):
        """Остановка всех запущенных процессов"""
        stopped = []
        
        if self.web_process and self.web_process.poll() is None:
            self.web_process.terminate()
            self.web_process.wait(timeout=5)
            self.web_process = None
            stopped.append("Веб-интерфейс")
            
        if self.gui_process and self.gui_process.poll() is None:
            self.gui_process.terminate()
            self.gui_process.wait(timeout=5)
            self.gui_process = None
            stopped.append("GUI настроек")
            
        if stopped:
            self.status_label.config(
                text=f"🛑 Остановлено: {', '.join(stopped)}", 
                fg="red"
            )
        else:
            self.status_label.config(text="Ничего не запущено", fg="gray")
            
    def on_closing(self):
        """Обработка закрытия окна"""
        # Завершаем дочерние процессы при закрытии
        if self.web_process and self.web_process.poll() is None:
            self.web_process.terminate()
        if self.gui_process and self.gui_process.poll() is None:
            self.gui_process.terminate()
        self.root.destroy()

def main():
    """Запуск стартовой панели"""
    root = tk.Tk()
    app = LauncherUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()

if __name__ == "__main__":
    main()
