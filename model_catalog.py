# model_catalog.py - единый каталог моделей: провайдеры LLM и OCR-модели
"""Списки моделей для CLI-лаунчера (run_cli.py) и веб-админки (/admin).

Раньше провайдеры и OCR-модели были прописаны только в run_cli.py: у админки
появился бы второй такой же список, и они бы разъехались. Здесь только константы —
ни torch, ни сети, ни БД, поэтому модуль дешёво импортируется и веб-сервером.

Модель векторизации (эмбеддинги) намеренно НЕ переключается: она прошита в
rag_core._get_embedding_model(), а её смена требует полной переиндексации всех
баз знаний. В админке она показывается справочно (EMBEDDING_MODEL).
"""

PROVIDERS = {
    "DashScope": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["deepseek-v4-flash", "qwen-plus", "qwen-turbo", "qwen-max"],
        "key_hint": "Ключ DashScope (llm_api_key) — он же используется для OCR, не удаляйте его.",
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "key_hint": "Отдельный ключ DeepSeek (llm_provider_api_key).",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["qwen/qwen-plus", "deepseek/deepseek-chat", "openai/gpt-4o-mini"],
        "key_hint": "Отдельный ключ OpenRouter (llm_openrouter_api_key).",
    },
}

OCR_MODELS = ["qwen-vl-ocr", "qwen-vl-plus", "qwen-vl-max"]

# Прошита в rag_core._get_embedding_model() — показывается в админке справочно.
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"

import re as _re


def ocr_model_slug(model):
    """Имя OCR-модели → безопасный сегмент пути кэша страниц.

    Модели содержат точки и «/» (qwen-vl-ocr, openai/gpt-4o и т.п.): в имени
    каталога это разделители, поэтому всё недопустимое заменяется на «_».
    Живёт здесь (а не в rag_core), чтобы тем же правилом пользовались и ядро,
    и служебные скрипты, которым импорт rag_core не нужен (torch, ~10 c).
    """
    slug = _re.sub(r'[^A-Za-z0-9._-]+', '_', str(model or 'default')).strip('._')
    return slug or 'default'


def provider_names():
    """Провайдеры LLM (порядок как в словаре)."""
    return list(PROVIDERS)


def models_for(provider):
    """Модели провайдера; пустой список — модель вводится вручную."""
    return list((PROVIDERS.get(provider) or {}).get("models", []))


def provider_base_url(provider):
    """OpenAI-совместимый base_url провайдера."""
    return (PROVIDERS.get(provider) or {}).get("base_url", "")
