import logging
import json
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional, Any, Dict

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

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
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    for open_ch, close_ch in [('{', '}'), ('[', ']')]:
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidate = cleaned[start:end+1]
            candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ValueError('No valid JSON found')


class BaseAdapter(ABC):
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3, timeout: Optional[float] = None):
        self.api_key = api_key
        self.model_name = model_name
        self.max_retries = max(1, int(max_retries))
        self.timeout = timeout

    @abstractmethod
    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        raise NotImplementedError

    def _retry_decorator(self):
        return retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )

    def generate_with_retry(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        return self._retry_decorator()(self.generate)(prompt, system_instruction, **kwargs)

    def generate_json(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> dict:
        text = self.generate_with_retry(prompt, system_instruction, **kwargs)
        return extract_json(text)


class GeminiAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash", **kwargs):
        super().__init__(api_key, model_name, **kwargs)
        if not HAS_GEMINI:
            raise ImportError("google-genai not installed")
        self.client = genai.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        config_kwargs = dict(kwargs.pop('generation_config', {}))
        if self.timeout is not None:
            config_kwargs.setdefault('timeout', self.timeout)
        if system_instruction:
            config_kwargs['system_instruction'] = system_instruction
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**config_kwargs)
        )
        return response.text or ''


class CohereAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "command-a-03-2025", **kwargs):
        super().__init__(api_key, model_name, **kwargs)
        if not HAS_COHERE:
            raise ImportError("cohere not installed")
        self.client = cohere.Client(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        if self.timeout is not None:
            kwargs.setdefault('timeout', self.timeout)
        response = self.client.chat(
            model=self.model_name,
            message=prompt,
            preamble=system_instruction,
            **kwargs
        )
        return getattr(response, 'text', '') or ''


class AnthropicAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "claude-sonnet-4-20250514", **kwargs):
        super().__init__(api_key, model_name, **kwargs)
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic not installed")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        max_tokens = kwargs.pop('max_tokens', 1024)
        if self.timeout is not None:
            kwargs.setdefault('timeout', self.timeout)
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=max_tokens,
            system=system_instruction or '',
            messages=[{'role': 'user', 'content': prompt}],
            **kwargs
        )
        parts = getattr(response, 'content', [])
        return parts[0].text if parts else ''


class GrokAdapter(BaseAdapter):
    def __init__(self, api_key: str, model_name: str = "grok-4.20-reasoning", **kwargs):
        super().__init__(api_key, model_name, **kwargs)
        if not HAS_OPENAI:
            raise ImportError("openai not installed")
        self.client = OpenAI(api_key=api_key, base_url='https://api.x.ai/v1')

    def generate(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        messages = []
        if system_instruction:
            messages.append({'role': 'system', 'content': system_instruction})
        messages.append({'role': 'user', 'content': prompt})
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content or ''


ADAPTERS = {
    'gemini': GeminiAdapter,
    'cohere': CohereAdapter,
    'anthropic': AnthropicAdapter,
    'grok': GrokAdapter,
}

DEFAULT_MODELS = {
    'gemini': 'gemini-2.5-flash',
    'cohere': 'command-a-03-2025',
    'anthropic': 'claude-sonnet-4-20250514',
    'grok': 'grok-4.20-reasoning',
}


def get_adapter(provider: str, api_key: str, model_name: Optional[str] = None, **kwargs) -> BaseAdapter:
    provider = provider.lower().strip()
    if provider not in ADAPTERS:
        raise ValueError(f'Unknown provider: {provider}')
    return ADAPTERS[provider](api_key, model_name or DEFAULT_MODELS[provider], **kwargs)