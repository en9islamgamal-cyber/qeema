"""
core_adapters.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
محولات النماذج (LLM Adapters)
• تفعيل الـ Native JSON Mode في Grok و Gemini.
• خوارزمية Bulletproof لاستخراج JSON من أي نص فوضوي.
"""

import logging # تم تصحيح حرف I ليكون صغيراً
import os
import json
import re
from abc import ABC, abstractmethod
from tenacity import retry, stop_after_attempt, wait_exponential

# استدعاء المكتبات بشكل آمن
try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    pass

try:
    import cohere
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError:
    pass

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """استخراج JSON بصلابة حتى لو هلوس الموديل بنصوص إضافية"""
    try:
        # 1. إزالة أي كود Markdown
        cleaned = re.sub(r"
http://googleusercontent.com/immersive_entry_chip/0
