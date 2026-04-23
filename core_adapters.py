"""
core_adapters.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
محولات النماذج (LLM Adapters) - متوافقة مع أي عدد من الوسائط الإضافية.
"""

import logging
import os
import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# استيراد المكتبات بشكل آمن
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    logger.warning("Google GenAI not installed.")

try:
    import cohere
    HAS_COHERE = True
except ImportError:
    HAS_COHERE = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# ========== دالة قوية لاستخراج JSON ==========
def extract_json(text: str) -> Dict[str, Any]:
    """استخراج JSON من النص الخام مع تحمل الأخطاء الشائعة."""
    if not text:
        raise json.JSONDecodeError("Empty text", text, 0)
    # إزالة علامات markdown
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    # البحث عن أول { وآخر }
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start == -1 or end == -1:
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start == -1 or end == -1:
            raise json.JSONDecodeError("No JSON object/array found", cleaned, 0)
    json_str = cleaned[start:end+1]
    # محاولة إصلاح الفواصل الزائدة
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    return json.loads(json_str)


# ========== الفئة الأساسية ==========
class BaseAdapter(ABC):
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3):
        self.api_key = api_key
        self.model_name = model_name
        self.max_retries = max_retries

    @abstractmethod
    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        """توليد رد نصي. *args و **kwargs لاستيعاب أي وسائط إضافية دون خطأ."""
        pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def generate_with_retry(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        return self.generate(prompt, system_instruction, *args, **kwargs)

    def generate_json(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> Dict[str, Any]:
        response = self.generate_with_retry(prompt, system_instruction, *args, **kwargs)
        return extract_json(response)


# ========== محول Gemini ==========
class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash-exp"):
        super().__init__(api_key, model_name)
        if not HAS_GEMINI:
            raise ImportError("google-genai not installed")
        self.client = genai.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        # args يتم تجاهلها، و kwargs قد تحتوي على generation_config إلخ
        generation_config = kwargs.pop("generation_config", {})
        if kwargs.get("response_mime_type") == "application/json":
            generation_config["response_mime_type"] = "application/json"
        sys_instr = None
        if system_instruction:
            sys_instr = genai_types.Content(role="user", parts=[genai_types.Part(text=system_instruction)])
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=sys_instr, **generation_config)
        )
        return response.text


# ========== محول Cohere ==========
class CohereAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "command-r-plus"):
        super().__init__(api_key, model_name)
        if not HAS_COHERE:
            raise ImportError("cohere not installed")
        self.client = cohere.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        # args و kwargs يمكن أن تحتوي على temperature, max_tokens إلخ
        response = self.client.chat(
            model=self.model_name,
            message=prompt,
            preamble=system_instruction,
            **kwargs
        )
        return response.text


# ========== محول Anthropic ==========
class AnthropicAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "claude-3-opus-20240229"):
        super().__init__(api_key, model_name)
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic not installed")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        max_tokens = kwargs.pop("max_tokens", 4096)
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=max_tokens,
            system=system_instruction or "",
            messages=[{"role": "user", "content": prompt}],
            **kwargs
        )
        return response.content[0].text


# ========== محول Grok ==========
class GrokAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "grok-beta"):
        super().__init__(api_key, model_name)
        if not HAS_OPENAI:
            raise ImportError("openai not installed")
        self.client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content


# ========== مصنع المحولات ==========
def get_adapter(provider: str, api_key: str, model_name: Optional[str] = None) -> BaseAdapter:
    provider = provider.lower()
    if provider == "gemini":
        return GeminiAdapter(api_key, model_name or "gemini-2.0-flash-exp")
    elif provider == "cohere":
        return CohereAdapter(api_key, model_name or "command-r-plus")
    elif provider == "anthropic":
        return AnthropicAdapter(api_key, model_name or "claude-3-opus-20240229")
    elif provider == "grok":
        return GrokAdapter(api_key, model_name or "grok-beta")
    else:
        raise ValueError(f"Unknown provider: {provider}")