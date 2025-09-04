import openai
import os
import csv
from sentence_transformers import util, SentenceTransformer
from rank_bm25 import BM25Okapi
import numpy as np
import pickle
import hashlib
import torch
from pathlib import Path
import PyPDF2
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import sys

# === Настройка клиента OpenRouter ===
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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"
)

# === Доступные модели ===
AVAILABLE_MODELS = [
    "mistralai/mistral-7b-instruct:free",
    "qwen/qwen3-30b-a3b-thinking-2507" #платный
]

class TerminalRedirect:
    """Класс для перенаправления вывода в терминал и GUI"""
    def __init__(self, text_widget=None):
        self.text_widget = text_widget
        self.terminal = sys.stdout
    
    def write(self, message):
        # Вывод в терминал
        self.terminal.write(message)
        self.terminal.flush()
        
        # Вывод в GUI (если виджет существует)
        if self.text_widget and hasattr(self.text_widget, 'winfo_exists') and self.text_widget.winfo_exists():
            try:
                self.text_widget.config(state='normal')
                self.text_widget.insert(tk.END, message)
                self.text_widget.config(state='disabled')
                self.text_widget.see(tk.END)
            except tk.TclError:
                pass  # Игнорируем ошибки GUI во время инициализации
    
    def flush(self):
        self.terminal.flush()

class RAGApplication:
    def __init__(self, root):
        self.root = root
        self.root.title("Система RAG с гибридным поиском")
        self.root.geometry("1000x700")
        
        # Переменные
        self.my_knowledge = []
        self.all_knowledge_dict = {}
        self.model = None
        self.corpus_embeddings = None
        self.bm25 = None
        self.tokenized_corpus = None
        
        self.create_widgets()
        
        # Перенаправление вывода (временно без GUI)
        self.terminal_redirect = TerminalRedirect()
        sys.stdout = self.terminal_redirect
        sys.stderr = self.terminal_redirect
        
        print("🚀 Запуск системы RAG с гибридным поиском...")
        
        # Инициализация модели в отдельном потоке
        self.setup_model()
        
    def create_widgets(self):
        # Создание вкладок
        tab_control = ttk.Notebook(self.root)
        
        # Вкладка чата
        self.chat_tab = ttk.Frame(tab_control)
        tab_control.add(self.chat_tab, text='Чат')
        
        # Вкладка управления базой знаний
        self.kb_tab = ttk.Frame(tab_control)
        tab_control.add(self.kb_tab, text='База знаний')
        
        tab_control.pack(expand=1, fill="both")
        
        # === Вкладка чата ===
        # Фрейм для отображения диалога
        chat_frame = ttk.Frame(self.chat_tab)
        chat_frame.pack(padx=10, pady=10, fill="both", expand=True)
        
        # Текстовое поле для диалога
        self.dialog_text = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, state='disabled')
        self.dialog_text.pack(fill="both", expand=True, pady=(0, 10))
        
        # Фрейм для ввода вопроса
        input_frame = ttk.Frame(self.chat_tab)
        input_frame.pack(padx=10, pady=(0, 10), fill="both", expand=True)
        
        # Многострочное поле ввода с переносом текста
        self.question_text = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, height=4)
        self.question_text.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.question_text.bind("<Control-Return>", self.ask_question)  # Ctrl+Enter для отправки
        self.question_text.bind("<Control-KeyPress>", self.handle_ctrl_key)  # Обработка Ctrl+C, Ctrl+V
        
        # Фрейм для кнопок
        buttons_frame = ttk.Frame(input_frame)
        buttons_frame.pack(side="right", fill="y")
        
        # Кнопка отправки вопроса
        self.ask_button = ttk.Button(buttons_frame, text="Отправить\n(Ctrl+Enter)", command=self.ask_question, width=12)
        self.ask_button.pack(pady=(0, 5))
        
        # Кнопка очистки поля ввода
        self.clear_input_button = ttk.Button(buttons_frame, text="Очистить\nввод", command=self.clear_input, width=12)
        self.clear_input_button.pack(pady=(0, 5))
        
        # Кнопка очистки диалога
        self.clear_dialog_button = ttk.Button(buttons_frame, text="Очистить\nдиалог", command=self.clear_dialog, width=12)
        self.clear_dialog_button.pack()
        
        # Подсказка
        hint_label = ttk.Label(self.chat_tab, 
                              text="Подсказка: Используйте Ctrl+Enter для отправки, Ctrl+C для копирования, Ctrl+V для вставки",
                              foreground="gray")
        hint_label.pack(pady=(0, 10))
        
        # === Вкладка базы знаний ===
        # Фрейм для управления файлами
        kb_control_frame = ttk.Frame(self.kb_tab)
        kb_control_frame.pack(padx=10, pady=10, fill="x")
        
        # Кнопка загрузки файлов
        self.load_button = ttk.Button(kb_control_frame, text="Загрузить файлы", command=self.load_files)
        self.load_button.pack(side="left", padx=(0, 5))
        
        # Кнопка перезагрузки базы знаний
        self.reload_button = ttk.Button(kb_control_frame, text="Перезагрузить базу знаний", command=self.reload_knowledge_base)
        self.reload_button.pack(side="left", padx=(0, 5))
        
        # Индикатор прогресса
        self.progress = ttk.Progressbar(kb_control_frame, mode='indeterminate')
        self.progress.pack(side="right", fill="x", expand=True)
        
        # Фрейм для отображения файлов
        files_frame = ttk.Frame(self.kb_tab)
        files_frame.pack(padx=10, pady=(0, 10), fill="both", expand=True)
        
        # Список загруженных файлов
        ttk.Label(files_frame, text="Загруженные файлы:").pack(anchor="w")
        self.files_listbox = tk.Listbox(files_frame)
        self.files_listbox.pack(fill="both", expand=True, pady=(5, 0))
        
        # Статусная строка
        self.status_var = tk.StringVar()
        self.status_var.set("Готов к работе")
        self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        
    def handle_ctrl_key(self, event):
        """Обработка Ctrl+C и Ctrl+V"""
        # Позволяем стандартные сочетания клавиш работать
        if event.keysym in ('c', 'v', 'a') and event.state & 0x4:  # Ctrl+C, Ctrl+V, Ctrl+A
            return  # Позволяем обработку по умолчанию
        return "break"  # Блокируем другие Ctrl+ комбинации
    
    def setup_model(self):
        """Инициализация модели и загрузка базы знаний"""
        def model_thread():
            try:
                print("🧠 Загрузка модели...")
                self.status_var.set("Загрузка модели...")
                self.model = SentenceTransformer('intfloat/multilingual-e5-large')
                print("✅ Модель загружена")
                
                # Обновляем перенаправление вывода, чтобы включить GUI
                self.terminal_redirect.text_widget = self.dialog_text
                
                self.reload_knowledge_base()
            except Exception as e:
                error_msg = f"❌ Не удалось загрузить модель: {e}"
                print(error_msg)
                self.root.after(0, lambda: messagebox.showerror("Ошибка", error_msg))
                self.status_var.set("Ошибка загрузки модели")
        
        threading.Thread(target=model_thread, daemon=True).start()
    
    def load_knowledge_from_txt(self, file_paths=None):
        """Загрузка знаний из файлов"""
        all_knowledge = {}
        
        # Если файлы не указаны, загружаем из стандартных мест
        if not file_paths:
            print("📂 Загрузка базы знаний из стандартных источников...")
            # Загрузка из DataBase.txt
            txt_file = "DataBase.txt"
            if os.path.exists(txt_file):
                print(f"📄 Обработка файла: {txt_file}")
                txt_knowledge = []
                try:
                    with open(txt_file, "r", encoding="utf-8") as file:
                        for i, line in enumerate(file, 1):
                            line = line.strip()
                            if line:
                                txt_knowledge.append(line)
                    if txt_knowledge:
                        print(f"✅ Загружено {len(txt_knowledge)} строк из {txt_file}")
                        all_knowledge[txt_file] = txt_knowledge
                    else:
                        print(f"⚠️ Файл {txt_file} пуст")
                except Exception as e:
                    print(f"❌ Ошибка при чтении {txt_file}: {e}")
            else:
                print(f"⚠️ Файл {txt_file} не найден")
            
            # Загрузка из папки Database
            database_folder = "Database"
            if os.path.exists(database_folder):
                print(f"📁 Обработка папки: {database_folder}")
                for file_name in os.listdir(database_folder):
                    file_path = os.path.join(database_folder, file_name)
                    self.process_file(file_path, all_knowledge)
            else:
                print(f"⚠️ Папка {database_folder} не найдена")
        else:
            # Загрузка указанных файлов
            print(f"📂 Загрузка {len(file_paths)} выбранных файлов...")
            for file_path in file_paths:
                print(f"📄 Обработка файла: {file_path}")
                self.process_file(file_path, all_knowledge)
        
        return all_knowledge
    
    def process_file(self, file_path, all_knowledge):
        """Обработка одного файла"""
        try:
            if file_path.lower().endswith(".txt"):
                with open(file_path, "r", encoding="utf-8") as file:
                    lines = [line.strip() for line in file if line.strip()]
                    if lines:
                        print(f"✅ Загружено {len(lines)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = lines
                    else:
                        print(f"⚠️ Файл {os.path.basename(file_path)} пуст")
                        
            elif file_path.lower().endswith(".csv"):
                with open(file_path, "r", encoding="utf-8") as csvfile:
                    reader = csv.DictReader(csvfile)
                    rows = list(reader)
                    csv_knowledge = []
                    for row in rows:
                        parts = []
                        for key, value in row.items():
                            clean_key = key.strip()
                            if clean_key.lower().startswith("unnamed"):
                                continue
                            if value and value.strip():
                                parts.append(f"{clean_key} — {value.strip()}")
                        
                        if parts:
                            if "Проект" in row and "Ответственный" in row:
                                entry = f"Проект {row['Проект']} находится в статусе «{row.get('Статус', 'не указан')}». Ответственный — {row['Ответственный']}."
                            elif "Имя" in row and "Должность" in row:
                                entry = f"{row['Имя']} работает {row['Должность']}. Контакт: email — {row.get('Email', 'не указан')}, телефон — {row.get('Телефон', 'не указан')}."
                            else:
                                entry = "В компании Аквесегмент: " + ", ".join(parts) + "."
                            csv_knowledge.append(entry)
                    if csv_knowledge:
                        print(f"✅ Загружено {len(csv_knowledge)} строк из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = csv_knowledge
                    else:
                        print(f"⚠️ Файл {os.path.basename(file_path)} не содержит данных")
                        
            elif file_path.lower().endswith(".pdf"):
                with open(file_path, 'rb') as f:
                    pdf_reader = PyPDF2.PdfReader(f)
                    print(f"📄 Обработка PDF файла: {os.path.basename(file_path)} (всего страниц: {len(pdf_reader.pages)})")
                    pdf_knowledge = []
                    for page_num, page in enumerate(pdf_reader.pages):
                        text = page.extract_text()
                        if text:
                            paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
                            for paragraph in paragraphs:
                                if len(paragraph) > 50:
                                    pdf_knowledge.append(f"[{os.path.basename(file_path)}, стр. {page_num+1}] {paragraph}")
                    if pdf_knowledge:
                        print(f"✅ Извлечено {len(pdf_knowledge)} фрагментов из {os.path.basename(file_path)}")
                        all_knowledge[file_path] = pdf_knowledge
                    else:
                        print(f"⚠️ Из файла {os.path.basename(file_path)} не удалось извлечь текст")
        except Exception as e:
            error_msg = f"❌ Ошибка при обработке файла {file_path}: {e}"
            print(error_msg)
    
    def get_file_embedding_cache_path(self, file_path, knowledge_content):
        """Генерирует путь к кэш-файлу для конкретного файла знаний"""
        content_hash = hashlib.md5(str(sorted(knowledge_content)).encode('utf-8')).hexdigest()
        cache_dir = Path("embeddings_cache")
        cache_dir.mkdir(exist_ok=True)
        filename = Path(file_path).stem
        cache_path = cache_dir / f"{filename}_{content_hash[:12]}.pkl"
        return cache_path

    def load_or_create_embeddings(self, file_path, knowledge_content):
        """Загружает эмбеддинги из кэша или создает их заново"""
        cache_path = self.get_file_embedding_cache_path(file_path, knowledge_content)
        
        if cache_path.exists():
            print(f"💾 Загрузка эмбеддингов из кэша для {os.path.basename(file_path)}...")
            with open(cache_path, 'rb') as f:
                embeddings = pickle.load(f)
            print(f"✅ Эмбеддинги загружены из кэша для {os.path.basename(file_path)}")
        else:
            print(f"🧠 Создание эмбеддингов для {os.path.basename(file_path)}...")
            embeddings = self.model.encode(knowledge_content, convert_to_tensor=True, show_progress_bar=True)
            with open(cache_path, 'wb') as f:
                pickle.dump(embeddings, f)
            print(f"✅ Эмбеддинги сохранены в кэш: {cache_path}")
        
        return embeddings
    
    def reload_knowledge_base(self):
        """Перезагрузка базы знаний"""
        def reload_thread():
            try:
                self.progress.start()
                self.status_var.set("Загрузка базы знаний...")
                print("\n🔄 Перезагрузка базы знаний...")
                
                # Загрузка знаний
                self.all_knowledge_dict = self.load_knowledge_from_txt()
                
                # Объединение знаний
                self.my_knowledge = []
                for file_path, knowledge_list in self.all_knowledge_dict.items():
                    self.my_knowledge.extend(knowledge_list)
                
                if not self.my_knowledge:
                    print("⚠️ База знаний пуста")
                    self.status_var.set("База знаний пуста")
                    return
                
                print(f"📊 Всего загружено {len(self.my_knowledge)} фрагментов знаний из {len(self.all_knowledge_dict)} файлов.")
                
                # Создание эмбеддингов
                print("🧠 Создание/загрузка эмбеддингов...")
                all_embeddings = []
                for file_path, knowledge_list in self.all_knowledge_dict.items():
                    file_embeddings = self.load_or_create_embeddings(file_path, knowledge_list)
                    all_embeddings.append(file_embeddings)
                
                if all_embeddings:
                    if len(all_embeddings) > 1:
                        self.corpus_embeddings = torch.cat(all_embeddings, dim=0)
                    else:
                        self.corpus_embeddings = all_embeddings[0]
                    
                    # Подготовка BM25
                    print("🔤 Подготовка лексического поиска (BM25)...")
                    self.tokenized_corpus = [doc.split(" ") for doc in self.my_knowledge]
                    self.bm25 = BM25Okapi(self.tokenized_corpus)
                    
                    # Обновление списка файлов в GUI
                    self.root.after(0, self.update_files_list)
                    
                    success_msg = f"✅ База знаний загружена: {len(self.my_knowledge)} фрагментов"
                    print(success_msg)
                    self.status_var.set(success_msg)
                else:
                    print("⚠️ Не удалось создать эмбеддинги")
                    self.status_var.set("Ошибка создания эмбеддингов")
                    
            except Exception as e:
                error_msg = f"❌ Ошибка загрузки базы знаний: {e}"
                print(error_msg)
                self.root.after(0, lambda: messagebox.showerror("Ошибка", error_msg))
                self.status_var.set("Ошибка загрузки базы знаний")
            finally:
                self.progress.stop()
        
        threading.Thread(target=reload_thread, daemon=True).start()
    
    def update_files_list(self):
        """Обновление списка файлов в интерфейсе"""
        self.files_listbox.delete(0, tk.END)
        for file_path in self.all_knowledge_dict.keys():
            self.files_listbox.insert(tk.END, os.path.basename(file_path))
    
    def load_files(self):
        """Загрузка файлов через диалог"""
        file_paths = filedialog.askopenfilenames(
            title="Выберите файлы",
            filetypes=[("Все поддерживаемые файлы", "*.txt *.csv *.pdf"), 
                      ("Текстовые файлы", "*.txt"),
                      ("CSV файлы", "*.csv"),
                      ("PDF файлы", "*.pdf"),
                      ("Все файлы", "*.*")]
        )
        
        if file_paths:
            def load_thread():
                try:
                    self.progress.start()
                    self.status_var.set("Загрузка файлов...")
                    print(f"\n📂 Загрузка {len(file_paths)} файлов...")
                    
                    # Загрузка выбранных файлов
                    new_knowledge = {}
                    for file_path in file_paths:
                        print(f"📄 Обработка файла: {file_path}")
                        self.process_file(file_path, new_knowledge)
                    
                    # Добавление к существующей базе знаний
                    self.all_knowledge_dict.update(new_knowledge)
                    
                    # Объединение знаний
                    self.my_knowledge = []
                    for file_path, knowledge_list in self.all_knowledge_dict.items():
                        self.my_knowledge.extend(knowledge_list)
                    
                    # Создание эмбеддингов для новых файлов
                    print("🧠 Создание/загрузка эмбеддингов для новых файлов...")
                    for file_path, knowledge_list in new_knowledge.items():
                        self.load_or_create_embeddings(file_path, knowledge_list)
                    
                    # Обновление BM25 и эмбеддингов
                    if self.my_knowledge:
                        all_embeddings = []
                        for file_path, knowledge_list in self.all_knowledge_dict.items():
                            file_embeddings = self.load_or_create_embeddings(file_path, knowledge_list)
                            all_embeddings.append(file_embeddings)
                        
                        if len(all_embeddings) > 1:
                            self.corpus_embeddings = torch.cat(all_embeddings, dim=0)
                        else:
                            self.corpus_embeddings = all_embeddings[0]
                        
                        self.tokenized_corpus = [doc.split(" ") for doc in self.my_knowledge]
                        self.bm25 = BM25Okapi(self.tokenized_corpus)
                        
                        # Обновление списка файлов в GUI
                        self.root.after(0, self.update_files_list)
                        
                        success_msg = f"✅ Файлы загружены: {len(self.my_knowledge)} фрагментов"
                        print(success_msg)
                        self.status_var.set(success_msg)
                    else:
                        print("⚠️ Нет данных для загрузки")
                        self.status_var.set("Нет данных для загрузки")
                        
                except Exception as e:
                    error_msg = f"❌ Ошибка загрузки файлов: {e}"
                    print(error_msg)
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", error_msg))
                    self.status_var.set("Ошибка загрузки файлов")
                finally:
                    self.progress.stop()
            
            threading.Thread(target=load_thread, daemon=True).start()
    
    def find_relevant_info(self, query, top_k=5, alpha=0.7):
        """Гибридный поиск"""
        print(f"\n🔍 Поиск релевантной информации для запроса: \"{query}\"")
        
        if not self.my_knowledge or self.corpus_embeddings is None or self.bm25 is None:
            print("⚠️ База знаний не загружена.")
            return "База знаний не загружена."
        
        # Семантический поиск
        print("🧠 Выполнение семантического поиска...")
        query_embedding = self.model.encode(query, convert_to_tensor=True)
        semantic_hits = util.semantic_search(query_embedding, self.corpus_embeddings, top_k=len(self.my_knowledge))
        semantic_scores = {hit['corpus_id']: hit['score'] for hit in semantic_hits[0]}

        # Лексический поиск (BM25)
        print("🔤 Выполнение лексического поиска (BM25)...")
        tokenized_query = query.split(" ")
        bm25_scores = self.bm25.get_scores(tokenized_query)
        
        # Нормализация BM25 оценок
        if np.max(bm25_scores) > 0:
            bm25_scores_norm = bm25_scores / np.max(bm25_scores)
        else:
            bm25_scores_norm = bm25_scores

        # Комбинирование оценок
        combined_scores = {}
        for i in range(len(self.my_knowledge)):
            sem_score = semantic_scores.get(i, 0.0)
            bm25_score = bm25_scores_norm[i]
            combined_scores[i] = alpha * sem_score + (1 - alpha) * bm25_score

        # Сортировка по комбинированной оценке
        sorted_indices = sorted(combined_scores.keys(), key=lambda x: combined_scores[x], reverse=True)
        
        # Выбор топ-K результатов
        top_indices = sorted_indices[:top_k]
        
        # Фильтрация по порогу
        best_score = combined_scores[top_indices[0]] if top_indices else 0
        if best_score < 0.2:
            print("⚠️ Низкая релевантность найденных данных.")
            return "Я не знаю ответа на этот вопрос."

        result = [self.my_knowledge[idx] for idx in top_indices]
        print(f"✅ Найдено {len(result)} релевантных фрагментов (лучший скор: {best_score:.3f})")
        return "\n".join(result)
    
    def ask_model(self, question):
        """Отправка запроса ко всем доступным моделям"""
        context = self.find_relevant_info(question, top_k=5, alpha=0.7)
        print(f"\n🔍 Переданный контекст моделям:\n{context}")
        
        answers = []
        
        for i, model_name in enumerate(AVAILABLE_MODELS):
            try:
                print(f"🤖 Отправка запроса к модели {model_name}...")
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": f"""
Ты — профессиональный помощник компании Аквесегмент. Отвечай кратко, ясно и строго на основе информации ниже.

Правила:
- Если ответа нет в информации — скажи «Я не знаю».
- Не выдумывай и не предполагай.
- Анализируй данные и формулируй ответ самостоятельно.
- Отвечай на негативные вопросы и отзывы в соответствии с ответами в базе знаний.
- Отвечай на позитивные вопросы и отзывы своими словами в виде слов благодарности за обращение в компанию.

Информация:
{context}
                        """},
                        {"role": "user", "content": question}
                    ]
                )
                answer = response.choices[0].message.content.strip()
                answers.append(f"Ответ ИИ модели {model_name}:\n{answer}\n")
                print(f"✅ Ответ получен от модели {model_name}")
            except Exception as e:
                error_msg = f"❌ Ошибка при обращении к модели {model_name}: {e}"
                print(error_msg)
                answers.append(f"Ответ ИИ модели {model_name}:\nОшибка: {error_msg}\n")
        
        return "\n---\n".join(answers)  # Разделяем ответы линией для лучшей читаемости
    
    def ask_question(self, event=None):
        """Обработка вопроса пользователя"""
        # Получаем текст из многострочного поля ввода
        question = self.question_text.get("1.0", tk.END).strip()
        if not question:
            return
        
        # Добавление вопроса в диалог
        self.append_to_dialog(f"Вы: {question}\n", "user")
        print(f"\n❓ Запрос: \"{question}\"")
        
        # Блокировка кнопки во время обработки
        self.ask_button.config(state="disabled")
        self.status_var.set("Обработка запроса...")
        
        def process_question():
            try:
                answer = self.ask_model(question)
                # Добавление ответа в диалог
                formatted_answer = f"{answer}\n\n"
                self.root.after(0, lambda: self.append_to_dialog(formatted_answer, "assistant"))
            except Exception as e:
                error_response = f"Ответ ИИ модели:\nОшибка: {e}\n\n"
                self.root.after(0, lambda: self.append_to_dialog(error_response, "error"))
            finally:
                self.root.after(0, lambda: self.ask_button.config(state="normal"))
                self.root.after(0, lambda: self.status_var.set("Готов к работе"))
        
        threading.Thread(target=process_question, daemon=True).start()
    
    def clear_input(self):
        """Очистка поля ввода"""
        self.question_text.delete("1.0", tk.END)
    
    def append_to_dialog(self, text, tag=None):
        """Добавление текста в диалог"""
        if hasattr(self.dialog_text, 'winfo_exists') and self.dialog_text.winfo_exists():
            try:
                self.dialog_text.config(state='normal')
                if tag:
                    self.dialog_text.insert(tk.END, text, tag)
                else:
                    self.dialog_text.insert(tk.END, text)
                self.dialog_text.config(state='disabled')
                self.dialog_text.see(tk.END)
                
                # Настройка тегов для цветового выделения
                self.dialog_text.tag_config("user", foreground="blue")
                self.dialog_text.tag_config("assistant", foreground="green")
                self.dialog_text.tag_config("error", foreground="red")
            except tk.TclError:
                pass  # Игнорируем ошибки GUI
    
    def clear_dialog(self):
        """Очистка диалога"""
        if hasattr(self.dialog_text, 'winfo_exists') and self.dialog_text.winfo_exists():
            try:
                self.dialog_text.config(state='normal')
                self.dialog_text.delete(1.0, tk.END)
                self.dialog_text.config(state='disabled')
            except tk.TclError:
                pass

def main():
    root = tk.Tk()
    app = RAGApplication(root)
    root.mainloop()

if __name__ == "__main__":
    main()