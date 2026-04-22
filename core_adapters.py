import logging
import os
import json
import re
from abc import ABC, abstractmethod
from tenacity import retry, stop_after_attempt, wait_exponential

# استدعاء المكتبات
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
    from openai import OpenAI # 👈 أضفنا مكتبة OpenAI التي تدعم Grok
except ImportError:
    pass

logger = logging.getLogger(__name__)

def extract_json(text: str) -> dict:
    """استخراج JSON من أي رد نصي فوضوي"""
    try:
        cleaned = re.sub(r"^\s*\x60{3}(?:json)?\s*", "", text, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*\x60{3}\s*$", "", cleaned, flags=re.MULTILINE)
        return json.loads(cleaned)
    except Exception as e:
        raise ValueError(f"فشل استخراج JSON: {str(e)}")

class BaseModelAdapter(ABC):
    @abstractmethod
    def generate(self, prompt: str, system_prompt: str, model_name: str) -> dict:
        pass

# 👇 محول Grok الجديد
class GrokAdapter(BaseModelAdapter):
    def __init__(self, api_key: str):
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1" # توجيه الطلبات لسيرفرات xAI
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def generate(self, prompt: str, system_prompt: str, model_name: str = "grok-2-latest") -> dict:
        response = self.client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return extract_json(response.choices[0].message.content)

class GeminiAdapter(BaseModelAdapter):
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def generate(self, prompt: str, system_prompt: str, model_name: str = "gemini-2.5-pro") -> dict:
        config = genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7
        )
        response = self.client.models.generate_content(
            model=model_name, contents=prompt, config=config
        )
        return extract_json(response.text)

class CohereAdapter(BaseModelAdapter):
    def __init__(self, api_key: str):
        self.client = cohere.Client(api_key=api_key)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def generate(self, prompt: str, system_prompt: str, model_name: str = "command-r-plus-08-2024") -> dict:
        response = self.client.chat(
            message=prompt, preamble=system_prompt, model=model_name, temperature=0.7
        )
        return extract_json(response.text)

class AnthropicAdapter(BaseModelAdapter):
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def generate(self, prompt: str, system_prompt: str, model_name: str = "claude-3-opus-20240229") -> dict:
        response = self.client.messages.create(
            model=model_name, max_tokens=4000, system=system_prompt,
            messages=[{"role": "user", "content": prompt}], temperature=0.7
        )
        return extract_json(response.content[0].text)