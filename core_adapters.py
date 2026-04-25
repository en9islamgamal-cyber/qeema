"""
core_adapters.py — VALUE / QEEMA v4.1 (Enterprise)
محولات النماذج اللغوية المتقدمة مع دعم JSON mode واستخراج ذكي وإعادة محاولة.

[CHANGELOG v4.1]
- إضافة: GroqAdapter (groq.com) — مجاني، سريع، يدعم JSON mode.
- تصحيح: GrokAdapter يبقى كما هو لـ x.ai (Grok الحقيقي) — مختلف تماماً عن Groq.
- تصحيح: جميع adapters تستقبل model_name كمعامل صريح في generate()
  بدل *args حتى لا يُتجاهل الموديل الممرر من script_engine.
- تحديث: get_adapter() يدعم provider="groq".
"""

import logging
import os
import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ─── استيراد المكتبات بشكل آمن ─────────────────────────────────────────────

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    logger.warning("google-genai غير مثبتة")

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


# ─── مساعد استخراج JSON ─────────────────────────────────────────────────────

def extract_json(text: str) -> Dict[str, Any]:
    """
    استخراج JSON من النص الخام مهما كان فوضويًا (بصلابة عالية).
    يدعم:
    - إزالة علامات markdown (```json ... ```)
    - البحث عن أول { وآخر } أو [ و ]
    - إصلاح الفواصل الزائدة
    """
    if not text:
        raise json.JSONDecodeError("Empty text", text, 0)

    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()

    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start == -1 or end == -1:
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start == -1 or end == -1:
            raise json.JSONDecodeError("No JSON object/array found", cleaned, 0)

    json_str = cleaned[start:end + 1]
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    return json.loads(json_str)


# ─── Base Adapter ─────────────────────────────────────────────────────────────

class BaseAdapter(ABC):
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3):
        self.api_key = api_key
        self.model_name = model_name  # الموديل الافتراضي
        self.max_retries = max_retries

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,  # ✅ يُستخدم لتجاوز self.model_name
        **kwargs
    ) -> str:
        """
        توليد رد نصي.
        model_name: إذا مُرِّر، يتجاوز self.model_name المضبوط في __init__.
        """
        pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def generate_with_retry(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        return self.generate(prompt, system_instruction, model_name, **kwargs)

    def generate_json(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        response = self.generate_with_retry(prompt, system_instruction, model_name, **kwargs)
        return extract_json(response)

    def _resolve_model(self, model_name: Optional[str]) -> str:
        """يعيد الموديل الممرر إن وُجد، وإلا يعيد الافتراضي."""
        return model_name if model_name else self.model_name


# ─── Gemini Adapter ──────────────────────────────────────────────────────────

class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        super().__init__(api_key, model_name)
        if not HAS_GEMINI:
            raise ImportError("google-genai غير مثبتة — pip install google-genai")
        self.client = genai.Client(api_key=api_key)

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        active_model = self._resolve_model(model_name)

        generation_config = kwargs.pop("generation_config", {})
        if kwargs.pop("response_mime_type", None) == "application/json":
            generation_config["response_mime_type"] = "application/json"

        sys_instr = None
        if system_instruction:
            sys_instr = genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=system_instruction)]
            )

        response = self.client.models.generate_content(
            model=active_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=sys_instr,
                **generation_config
            )
        )
        return response.text


# ─── Cohere Adapter ──────────────────────────────────────────────────────────

class CohereAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "command-r-plus-08-2024"):
        super().__init__(api_key, model_name)
        if not HAS_COHERE:
            raise ImportError("cohere غير مثبتة — pip install cohere")
        self.client = cohere.Client(api_key=api_key)

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        active_model = self._resolve_model(model_name)
        response = self.client.chat(
            model=active_model,
            message=prompt,
            preamble=system_instruction,
            **kwargs
        )
        return response.text


# ─── Anthropic Adapter ───────────────────────────────────────────────────────

class AnthropicAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "claude-3-opus-20240229"):
        super().__init__(api_key, model_name)
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic غير مثبتة — pip install anthropic")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        active_model = self._resolve_model(model_name)
        max_tokens = kwargs.pop("max_tokens", 4096)
        response = self.client.messages.create(
            model=active_model,
            max_tokens=max_tokens,
            system=system_instruction or "",
            messages=[{"role": "user", "content": prompt}],
            **kwargs
        )
        return response.content[0].text


# ─── Groq Adapter ────────────────────────────────────────────────────────────
# groq.com — مجاني، سريع (300+ token/sec)، OpenAI-compatible
# المفتاح يبدأ بـ: gsk_  — من console.groq.com
# الموديلات المتاحة مجاناً:
#   llama-3.3-70b-versatile  ← الأفضل للنصوص العربية الطويلة
#   llama-3.1-8b-instant     ← أسرع، للمهام الخفيفة
#   gemma2-9b-it             ← بديل خفيف

class GroqAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "llama-3.3-70b-versatile"):
        super().__init__(api_key, model_name)
        if not HAS_OPENAI:
            raise ImportError("openai غير مثبتة — pip install openai")
        if not api_key or not api_key.startswith("gsk_"):
            raise ValueError(
                "❌ مفتاح Groq غير صحيح — يجب أن يبدأ بـ gsk_\n"
                "   احصل على مفتاح مجاني من: https://console.groq.com"
            )
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1"   # ✅ groq.com وليس x.ai
        )
        logger.info(f"✅ GroqAdapter جاهز — الموديل الافتراضي: {model_name}")

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        active_model = self._resolve_model(model_name)
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=active_model,
            messages=messages,
            temperature=kwargs.pop("temperature", 0.7),
            max_tokens=kwargs.pop("max_tokens", 8000),
            response_format={"type": "json_object"},  # Groq يدعم JSON mode
            **kwargs
        )
        return response.choices[0].message.content


# ─── Grok Adapter (x.ai) ─────────────────────────────────────────────────────
# هذا هو Grok الحقيقي من شركة xAI (إيلون ماسك) — مختلف تماماً عن Groq
# API endpoint: api.x.ai — مدفوع، ليس مجانياً
# المفتاح من: console.x.ai

class GrokAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "grok-3-beta"):
        super().__init__(api_key, model_name)
        if not HAS_OPENAI:
            raise ImportError("openai غير مثبتة — pip install openai")
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1"   # x.ai وليس groq.com
        )

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs
    ) -> str:
        active_model = self._resolve_model(model_name)
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        response = self.client.chat.completions.create(
            model=active_model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content


# ─── Factory Function ─────────────────────────────────────────────────────────

def get_adapter(
    provider: str,
    api_key: str,
    model_name: Optional[str] = None
) -> BaseAdapter:
    """
    يُنشئ الـ adapter المناسب حسب اسم المزود.

    المزودون المدعومون:
      gemini    → Google Gemini (google-genai)
      cohere    → Cohere Command R
      anthropic → Claude (Anthropic)
      groq      → Groq — مجاني، سريع (llama-3.3-70b) ✅ جديد
      grok      → xAI Grok — مدفوع (api.x.ai)
    """
    provider = provider.lower().strip()

    if provider == "gemini":
        return GeminiAdapter(api_key, model_name or "gemini-2.5-flash")

    elif provider == "cohere":
        return CohereAdapter(api_key, model_name or "command-r-plus-08-2024")

    elif provider == "anthropic":
        return AnthropicAdapter(api_key, model_name or "claude-3-opus-20240229")

    elif provider == "groq":
        # groq.com — مجاني، OpenAI-compatible
        return GroqAdapter(api_key, model_name or "llama-3.3-70b-versatile")

    elif provider == "grok":
        # x.ai — مدفوع، Grok من xAI
        return GrokAdapter(api_key, model_name or "grok-3-beta")

    else:
        raise ValueError(
            f"❌ مزود غير معروف: '{provider}'\n"
            f"   المزودون المدعومون: gemini, cohere, anthropic, groq, grok"
        )
