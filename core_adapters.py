"""
core_adapters.py — VALUE / QEEMA v3.0 (تم التحديث للنماذج الحديثة)
"""

import logging
import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# استيراد المكتبات
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    logger.warning("google-genai not installed")

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

def extract_json(text: str) -> dict:
    """استخراج JSON من النص الخام (مقاوم للفوضى)"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start == -1 or end == -1:
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start == -1 or end == -1:
            raise ValueError("No JSON found")
    json_str = cleaned[start:end+1]
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    return json.loads(json_str)

class BaseAdapter(ABC):
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3):
        self.api_key = api_key
        self.model_name = model_name
        self.max_retries = max_retries

    @abstractmethod
    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def generate_with_retry(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        return self.generate(prompt, system_instruction, *args, **kwargs)

    def generate_json(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> dict:
        return extract_json(self.generate_with_retry(prompt, system_instruction, *args, **kwargs))

# ----------------------------------------------------------------------
# Gemini (تم التحديد إلى gemini-2.5-flash وهو النموذج المستقر حاليًا)
# ----------------------------------------------------------------------
class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        super().__init__(api_key, model_name)
        if not HAS_GEMINI:
            raise ImportError("google-genai not installed")
        self.client = genai.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        generation_config = kwargs.pop("generation_config", {})
        sys_instr = None
        if system_instruction:
            sys_instr = genai_types.Content(role="user", parts=[genai_types.Part(text=system_instruction)])
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=sys_instr, **generation_config)
        )
        return response.text

# ----------------------------------------------------------------------
# Cohere (أحدث نموذج متوفر command-a-03-2025)
# ----------------------------------------------------------------------
class CohereAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "command-a-03-2025"):
        super().__init__(api_key, model_name)
        if not HAS_COHERE:
            raise ImportError("cohere not installed")
        self.client = cohere.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, *args, **kwargs) -> str:
        response = self.client.chat(
            model=self.model_name,
            message=prompt,
            preamble=system_instruction,
            **kwargs
        )
        return response.text

# ----------------------------------------------------------------------
# Anthropic (يتطلب رصيدًا كافيًا في الحساب)
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Grok (آخر نموذج متوفر في x.ai)
# ----------------------------------------------------------------------
class GrokAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "grok-4.20-beta-0309-non-reasoning"):
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

# ----------------------------------------------------------------------
# مصنع المحولات
# ----------------------------------------------------------------------
def get_adapter(provider: str, api_key: str, model_name: Optional[str] = None) -> BaseAdapter:
    provider = provider.lower()
    if provider == "gemini":
        return GeminiAdapter(api_key, model_name or "gemini-2.5-flash")
    elif provider == "cohere":
        return CohereAdapter(api_key, model_name or "command-a-03-2025")
    elif provider == "anthropic":
        return AnthropicAdapter(api_key, model_name or "claude-3-opus-20240229")
    elif provider == "grok":
        return GrokAdapter(api_key, model_name or "grok-4.20-beta-0309-non-reasoning")
    else:
        raise ValueError(f"Unknown provider: {provider}")