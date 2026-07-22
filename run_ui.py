# run_ui.py - Стартовая панель для запуска компонентов RAG-системы
"""Стартовая панель с выбором LLM провайдера/модели и кнопками запуска"""

import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import sys
import os
import webbrowser
import threading
import json

# Маппинг провайдеров: base_url и доступные модели
PROVIDERS = {
    "DashScope": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["deepseek-v4-flash", "qwen-plus", "qwen-turbo", "qwen-max"]
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["qwen/qwen-plus", "deepseek/deepseek-chat", "openai/gpt-4o-mini"]
    }
}

class LauncherUI:
    def __init__(self, root):
        self.root = root
        self.root.title("RAG-система - Стартовая панель")
        self.root.geometry("420x480")
        self.root.resizable(False, False)
        
        # Путь к директории проекта
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Хранилище запущенных процессов
        self.web_process = None
        self.gui_process = None
        
        # Загрузка текущих настроек
        self.settings = self.load_settings()
        
        self.setup_ui()
        
    def load_settings(self):
        """Загрузка настроек из rag_settings.json"""
        settings_file = os.path.join(self.project_dir, "rag_settings.json")
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    
    def save_settings(self, provider, model):
        """Сохранение выбранного провайдера и модели в настройки"""
        settings_file = os.path.join(self.project_dir, "rag_settings.json")
        provider_config = PROVIDERS.get(provider, PROVIDERS["DashScope"])
        
        self.settings["llm_provider"] = provider
        self.settings["llm_model"] = model
        self.settings["llm_base_url"] = provider_config["base_url"]
        
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить настройки:\n{e}")
        
    def setup_ui(self):
        """Настройка интерфейса"""
        # Заголовок
        title_label = tk.Label(
            self.root, 
            text="🚀 RAG-система", 
            font=("Arial", 18, "bold")
        )
        title_label.pack(pady=10)
        
        subtitle_label = tk.Label(
            self.root,
            text="Выберите провайдера, модель и запустите компонент",
            font=("Arial", 10)
        )
        subtitle_label.pack(pady=3)
        
        # === Блок выбора провайдера и модели ===
        config_frame = ttk.LabelFrame(self.root, text="🤖 Настройки LLM", padding=10)
        config_frame.pack(fill="x", padx=20, pady=10)
        
        # Провайдер
        ttk.Label(config_frame, text="Провайдер:").grid(row=0, column=0, sticky="w", pady=3)
        current_provider = self.settings.get("llm_provider", "DashScope")
        self.provider_var = tk.StringVar(value=current_provider)
        self.provider_combo = ttk.Combobox(
            config_frame, 
            textvariable=self.provider_var, 
            values=list(PROVIDERS.keys()), 
            state="readonly", 
            width=30
        )
        self.provider_combo.grid(row=0, column=1, sticky="ew", pady=3, padx=(5, 0))
        self.provider_combo.bind("<<ComboboxSelected>>", self.on_provider_change)
        
        # Модель
        ttk.Label(config_frame, text="Модель:").grid(row=1, column=0, sticky="w", pady=3)
        current_model = self.settings.get("llm_model", "deepseek-v4-flash")
        self.model_var = tk.StringVar(value=current_model)
        self.model_combo = ttk.Combobox(
            config_frame, 
            textvariable=self.model_var, 
            values=PROVIDERS.get(current_provider, PROVIDERS["DashScope"])["models"],
            state="readonly",
            width=30
        )
        self.model_combo.grid(row=1, column=1, sticky="ew", pady=3, padx=(5, 0))
        
        config_frame.columnconfigure(1, weight=1)
        
        # Разделитель
        separator = tk.Frame(self.root, height=2, bd=1, relief=tk.SUNKEN)
        separator.pack(fill=tk.X, padx=20, pady=8)
        
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
        self.web_button.pack(pady=6)
        
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
        self.gui_button.pack(pady=6)
        
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
        self.stop_button.pack(pady=6)
        
        # Статус бар
        self.status_label = tk.Label(
            self.root,
            text=f"Готов к запуску | {current_provider} / {current_model}",
            font=("Arial", 9),
            fg="gray"
        )
        self.status_label.pack(side=tk.BOTTOM, pady=8)
    
    def on_provider_change(self, event=None):
        """Обновление списка моделей при смене провайдера"""
        provider = self.provider_var.get()
        models = PROVIDERS.get(provider, PROVIDERS["DashScope"])["models"]
        self.model_combo['values'] = models
        self.model_var.set(models[0])
        
    def apply_llm_settings(self):
        """Сохранить выбранные настройки провайдера/модели"""
        provider = self.provider_var.get()
        model = self.model_var.get()
        self.save_settings(provider, model)
        self.status_label.config(text=f"✅ {provider} / {model}", fg="green")
        
    def launch_web(self):
        """Запуск веб-интерфейса"""
        if self.web_process and self.web_process.poll() is None:
            messagebox.showinfo("Информация", "Веб-интерфейс уже запущен!")
            return
            
        try:
            # Сохраняем выбранные настройки перед запуском
            self.apply_llm_settings()
            
            python_exe = sys.executable
            script_path = os.path.join(self.project_dir, "run_web.py")
            
            self.web_process = subprocess.Popen(
                [python_exe, script_path],
                cwd=self.project_dir
            )
            
            self.status_label.config(
                text=f"✅ Веб-интерфейс запущен (порт 8077) | {self.provider_var.get()} / {self.model_var.get()}", 
                fg="green"
            )
            
            # Автоматическое открытие браузера через задержку
            def open_browser():
                import time
                time.sleep(2)
                webbrowser.open("http://localhost:8077")
            
            threading.Thread(target=open_browser, daemon=True).start()
            
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить веб-интерфейс:\n{str(e)}")
            self.status_label.config(text="❌ Ошибка запуска", fg="red")
            
    def launch_gui(self):
        """Запуск GUI настроек"""
        if self.gui_process and self.gui_process.poll() is None:
            messagebox.showinfo("Информация", "GUI настроек уже запущен!")
            return
            
        try:
            # Сохраняем выбранные настройки перед запуском
            self.apply_llm_settings()
            
            python_exe = sys.executable
            script_path = os.path.join(self.project_dir, "run_gui.py")
            
            self.gui_process = subprocess.Popen(
                [python_exe, script_path],
                cwd=self.project_dir
            )
            
            self.status_label.config(
                text=f"✅ GUI настроек запущен | {self.provider_var.get()} / {self.model_var.get()}", 
                fg="blue"
            )
            
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить GUI настроек:\n{str(e)}")
            self.status_label.config(text="❌ Ошибка запуска", fg="red")
            
    def stop_all(self):
        """Остановка всех запущенных процессов"""
        stopped = []
        
        if self.web_process and self.web_process.poll() is None:
            self.web_process.terminate()
            try:
                self.web_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.web_process.kill()
            self.web_process = None
            stopped.append("Веб-интерфейс")
            
        if self.gui_process and self.gui_process.poll() is None:
            self.gui_process.terminate()
            try:
                self.gui_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.gui_process.kill()
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
