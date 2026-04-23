"""
core_adapters.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
محولات النماذج (LLM Adapters)
• تفعيل الـ Native JSON Mode في Grok و Gemini.
• خوارزمية Bulletproof لاستخراج JSON من أي نص فوضوي.
"""

import logging
import os
import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

from tenacity import retry, stop_after_attempt, wait_exponential

# تسجيل الأحداث
logger = logging.getLogger(__name__)

# استيراد المكتبات الخارجية بشكل آمن مع رسائل تحذير عند الفشل
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    logger.warning("Google GenAI not installed. GeminiAdapter will not work.")

try:
    import cohere
    HAS_COHERE = True
except ImportError:
    HAS_COHERE = False
    logger.warning("Cohere not installed. CohereAdapter will not work.")

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    logger.warning("Anthropic not installed. AnthropicAdapter will not work.")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    logger.warning("OpenAI not installed. GrokAdapter may not work.")


# ============================================================================
# دالة قوية لاستخراج JSON من النص (Bulletproof)
# ============================================================================
def extract_json(text: str) -> Dict[str, Any]:
    """
    استخراج JSON من النص الخام حتى لو كان محاطاً بنصوص عشوائية أو Markdown.
    
    الخوارزمية:
        1. إزالة علامات Markdown الخاصة بالكود (```json, ```, etc.)
        2. البحث عن أول { وآخر } في النص بعد التنظيف الأولي.
        3. محاولة تحميل JSON باستخدام json.loads.
        4. إذا فشل، حاول إصلاح الأخطاء الشائعة (فواصل إضافية، تعليقات، إلخ).
        5. في حالة فشل كل شيء، يرفع استثناء JSONDecodeError.
    
    Args:
        text: النص الخام القادم من النموذج
    
    Returns:
        قاموس JSON
    
    Raises:
        json.JSONDecodeError: إذا تعذر استخراج JSON صالح.
    """
    if not text:
        raise json.JSONDecodeError("Empty text provided", text, 0)
    
    original_text = text
    # 1. إزالة أوسمة الكود Markdown الشائعة
    cleaned = re.sub(r"```(?:json)?\s*", "", text)          # إزالة ```json و ``` في البداية
    cleaned = re.sub(r"\s*```", "", cleaned)                # إزالة ``` في النهاية
    cleaned = cleaned.strip()
    
    # 2. البحث عن أقواس JSON
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    
    if start == -1 or end == -1 or end <= start:
        # قد يكون الموديل أخرج مصفوفة JSON بدلاً من كائن
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start == -1 or end == -1 or end <= start:
            raise json.JSONDecodeError(f"No JSON object or array found in response: {cleaned[:200]}", cleaned, 0)
    
    json_str = cleaned[start:end+1]
    
    # 3. محاولة التحميل المباشر
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"Direct JSON parsing failed: {e}. Attempting to repair common issues.")
        
        # 4. محاولات إصلاح بسيطة
        # إزالة الفواصل الزائدة قبل الأقواس أو الأقواس المغلقة
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        # إزالة التعليقات المفردة // (إن وجدت)
        json_str = re.sub(r'//.*?(\n|$)', '', json_str)
        # إزالة trailing comma في آخر كائن أو مصفوفة
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # فشل الإصلاح - سجل النص الأصلي للمساعدة في التصحيح
            logger.error(f"Failed to extract JSON even after repair. Original snippet: {original_text[:500]}")
            raise


# ============================================================================
# الفئة الأساسية لجميع المحولات
# ============================================================================
class BaseAdapter(ABC):
    """واجهة موحدة لجميع محولات LLM"""
    
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3):
        self.api_key = api_key
        self.model_name = model_name
        self.max_retries = max_retries
        self._validate_api_key()
    
    def _validate_api_key(self):
        if not self.api_key or len(self.api_key) < 10:
            logger.warning(f"API key for {self.__class__.__name__} appears invalid or missing.")
    
    @abstractmethod
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        """توليد رد نصي من النموذج"""
        pass
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def generate_with_retry(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        """توليد مع إعادة محاولة تلقائية عند الفشل"""
        return self.generate(prompt, system_instruction, **kwargs)
    
    def generate_json(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """
        توليد رد وتوقع أنه بصيغة JSON، ثم استخراجه.
        """
        response_text = self.generate_with_retry(prompt, system_instruction, **kwargs)
        return extract_json(response_text)


# ============================================================================
# محول Gemini (يدعم JSON mode أصلياً)
# ============================================================================
class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash-exp"):
        super().__init__(api_key, model_name)
        if not HAS_GEMINI:
            raise ImportError("Google GenAI library not installed. Please install 'google-genai'.")
        self.client = genai.Client(api_key=api_key)
    
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        """توليد باستخدام Gemini مع دعم JSON mode"""
        # إعدادات الـ generation
        generation_config = kwargs.pop("generation_config", {})
        if kwargs.get("response_mime_type") == "application/json":
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = kwargs.get("response_schema", None)
        
        # إعداد التعليمات النظامية
        sys_instr = None
        if system_instruction:
            sys_instr = genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=system_instruction)]
            )
        
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=sys_instr,
                **generation_config
            )
        )
        return response.text


# ============================================================================
# محول Cohere
# ============================================================================
class CohereAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "command-r-plus"):
        super().__init__(api_key, model_name)
        if not HAS_COHERE:
            raise ImportError("Cohere library not installed. Please install 'cohere'.")
        self.client = cohere.Client(api_key=api_key)
    
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        response = self.client.chat(
            model=self.model_name,
            message=prompt,
            preamble=system_instruction,
            **kwargs
        )
        return response.text


# ============================================================================
# محول Anthropic Claude
# ============================================================================
class AnthropicAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "claude-3-opus-20240229"):
        super().__init__(api_key, model_name)
        if not HAS_ANTHROPIC:
            raise ImportError("Anthropic library not installed. Please install 'anthropic'.")
        self.client = anthropic.Anthropic(api_key=api_key)
    
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system_instruction if system_instruction else "",
            messages=[{"role": "user", "content": prompt}],
            **{k: v for k, v in kwargs.items() if k not in ["max_tokens"]}
        )
        return response.content[0].text


# ============================================================================
# محول Grok (عبر واجهة OpenAI)
# ============================================================================
class GrokAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "grok-beta"):
        super().__init__(api_key, model_name)
        if not HAS_OPENAI:
            raise ImportError("OpenAI library not installed. Please install 'openai'.")
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",   # نقطة نهاية Grok
        )
    
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
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


# ============================================================================
# دالة مساعدة لإنشاء المحول المناسب بناءً على الاسم
# ============================================================================
def get_adapter(provider: str, api_key: str, model_name: Optional[str] = None) -> BaseAdapter:
    """
    مصنع بسيط لإرجاع نسخة من المحول المطلوب.
    
    Args:
        provider: "gemini", "cohere", "anthropic", "grok"
        api_key: مفتاح API الخاص بالمزود
        model_name: اسم النموذج (اختياري، يستخدم الافتراضي إن لم يحدد)
    
    Returns:
        كائن المحول
    
    Raises:
        ValueError: إذا كان المزود غير معروف أو كانت المكتبة غير مثبتة.
    """
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
        raise ValueError(f"Unknown provider: {provider}. Supported: gemini, cohere, anthropic, grok")


# ============================================================================
# حالة اختبار سريعة (للتأكد من عمل extract_json)
# ============================================================================
if __name__ == "__main__":
    # اختبار extract_json
    test_inputs = [
        '{"key": "value"}',
        '```json\n{"key": "value"}\n```',
        'Some text before {"key": "value"} and after.',
        '```\n{"key": "value",}\n```',   # مع فاصلة زائدة
        '[1, 2, 3]',                     # مصفوفة
        '{"nested": {"foo": "bar"}}',
    ]
    for i, inp in enumerate(test_inputs, 1):
        try:
            result = extract_json(inp)
            print(f"✅ Test {i} passed: {result}")
        except Exception as e:
            print(f"❌ Test {i} failed: {e}")