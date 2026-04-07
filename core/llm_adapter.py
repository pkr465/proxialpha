"""
Universal LLM Adapter - Unified interface for Claude, OpenAI, Ollama (local), and custom providers.
Every strategy can call self.llm.analyze() to get AI-powered insights.

Supports:
  - Anthropic Claude (claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5)
  - OpenAI GPT (gpt-4o, gpt-4-turbo, gpt-3.5-turbo)
  - Ollama Local (llama3, mistral, codellama, mixtral, phi, etc.)
  - Google Gemini (gemini-pro, gemini-1.5-pro)
  - Custom HTTP endpoints (any OpenAI-compatible API)

Usage:
    llm = LLMAdapter(provider="claude", model="claude-sonnet-4-6", api_key="sk-...")
    llm = LLMAdapter(provider="openai", model="gpt-4o", api_key="sk-...")
    llm = LLMAdapter(provider="ollama", model="llama3", base_url="http://localhost:11434")
    llm = LLMAdapter(provider="custom", model="my-model", base_url="http://my-server:8080/v1")

    result = llm.analyze(market_data, prompt="Analyze this stock for entry points")
    signals = llm.generate_signals(ticker, technical_data)
"""
import json
import os
from typing import Optional
from dataclasses import dataclass


@dataclass
class LLMConfig:
    provider: str = "claude"           # claude, openai, ollama, gemini, custom
    model: str = "claude-sonnet-4-6"
    api_key: Optional[str] = None
    base_url: Optional[str] = None     # For ollama/custom: http://localhost:11434
    max_tokens: int = 2048
    temperature: float = 0.3
    timeout: int = 30
    system_prompt: str = ""
    fallback_provider: Optional[str] = None  # Fallback if primary fails


class LLMResponse:
    def __init__(self, text: str, model: str, provider: str, usage: dict = None):
        self.text = text
        self.model = model
        self.provider = provider
        self.usage = usage or {}

    def to_json(self) -> dict | None:
        """Try to parse response as JSON."""
        import re
        try:
            match = re.search(r'[\[{][\s\S]*[\]}]', self.text)
            if match:
                return json.loads(match.group())
        except json.JSONDecodeError:
            pass
        return None


class LLMAdapter:
    """
    Universal LLM adapter with provider abstraction.
    Strategies call this instead of provider-specific SDKs.
    """

    TRADING_SYSTEM_PROMPT = """You are a quantitative trading analyst AI. Analyze market data and
generate precise, actionable trading signals. Always respond with valid JSON.
Consider: technical indicators, price action, volume, momentum, risk/reward ratio,
and sector context. Be conservative with position sizing."""

    def __init__(self, config: LLMConfig = None, **kwargs):
        if config:
            self.config = config
        else:
            self.config = LLMConfig(**kwargs)

        if not self.config.system_prompt:
            self.config.system_prompt = self.TRADING_SYSTEM_PROMPT

        # Auto-detect API keys from env vars
        if not self.config.api_key:
            env_map = {
                'claude': 'ANTHROPIC_API_KEY',
                'openai': 'OPENAI_API_KEY',
                'gemini': 'GOOGLE_API_KEY',
            }
            env_var = env_map.get(self.config.provider, '')
            self.config.api_key = os.environ.get(env_var)

        # Default base URLs
        if not self.config.base_url:
            url_map = {
                'ollama': 'http://localhost:11434',
                'custom': 'http://localhost:8080/v1',
            }
            self.config.base_url = url_map.get(self.config.provider)

        self._client = None

    def analyze(self, context: str, prompt: str = "Analyze this data and provide insights") -> LLMResponse:
        """Send a prompt with context to the LLM and get a response."""
        full_prompt = f"{prompt}\n\nDATA:\n{context}"

        try:
            return self._call_provider(full_prompt)
        except Exception as e:
            # Try fallback provider
            if self.config.fallback_provider:
                try:
                    fallback = LLMAdapter(LLMConfig(
                        provider=self.config.fallback_provider,
                        model=self._default_model(self.config.fallback_provider),
                        system_prompt=self.config.system_prompt,
                    ))
                    return fallback._call_provider(full_prompt)
                except Exception:
                    pass
            return LLMResponse(text=f"Error: {e}", model=self.config.model, provider=self.config.provider)

    def generate_signals(self, ticker: str, data: dict) -> dict:
        """Generate trading signals for a ticker using LLM analysis."""
        prompt = f"""Analyze {ticker} and generate a trading signal.
Respond ONLY with a JSON object in this exact format:
{{
    "ticker": "{ticker}",
    "signal": "BUY" or "SELL" or "HOLD" or "STRONG_BUY" or "STRONG_SELL",
    "confidence": 0.0-1.0,
    "target_price": number or null,
    "stop_loss": number or null,
    "position_size_pct": 0.01-0.10,
    "reasoning": "brief explanation"
}}"""
        response = self.analyze(json.dumps(data, indent=2, default=str), prompt)
        parsed = response.to_json()
        return parsed if parsed else {'ticker': ticker, 'signal': 'HOLD', 'confidence': 0, 'reasoning': response.text[:200]}

    def optimize_strategy(self, strategy_name: str, params: dict, metrics: dict) -> dict:
        """Ask LLM to suggest strategy parameter optimizations."""
        prompt = f"""Review the performance of the '{strategy_name}' strategy and suggest parameter improvements.

Current Parameters: {json.dumps(params, indent=2)}
Performance Metrics: {json.dumps(metrics, indent=2)}

Respond with JSON:
{{
    "parameter_changes": [{{"param": "key", "old": value, "new": value, "reasoning": "why"}}],
    "overall_assessment": "paragraph"
}}"""
        response = self.analyze("", prompt)
        return response.to_json() or {'parameter_changes': [], 'overall_assessment': response.text[:500]}

    def _call_provider(self, prompt: str) -> LLMResponse:
        """Route to the correct provider."""
        providers = {
            'claude': self._call_claude,
            'openai': self._call_openai,
            'ollama': self._call_ollama,
            'gemini': self._call_gemini,
            'custom': self._call_openai_compatible,
        }
        handler = providers.get(self.config.provider)
        if not handler:
            raise ValueError(f"Unknown provider: {self.config.provider}")
        return handler(prompt)

    def _call_claude(self, prompt: str) -> LLMResponse:
        from anthropic import Anthropic
        if not self._client:
            self._client = Anthropic(api_key=self.config.api_key)
        response = self._client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=self.config.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return LLMResponse(
            text=response.content[0].text,
            model=self.config.model, provider="claude",
            usage={'input_tokens': response.usage.input_tokens, 'output_tokens': response.usage.output_tokens},
        )

    def _call_openai(self, prompt: str) -> LLMResponse:
        from openai import OpenAI
        if not self._client:
            self._client = OpenAI(api_key=self.config.api_key)
        response = self._client.chat.completions.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            messages=[
                {"role": "system", "content": self.config.system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        choice = response.choices[0]
        return LLMResponse(
            text=choice.message.content,
            model=self.config.model, provider="openai",
            usage={'total_tokens': response.usage.total_tokens} if response.usage else {},
        )

    def _call_ollama(self, prompt: str) -> LLMResponse:
        """Call Ollama local LLM. No API key needed."""
        import urllib.request
        url = f"{self.config.base_url}/api/generate"
        data = json.dumps({
            "model": self.config.model,
            "prompt": f"System: {self.config.system_prompt}\n\nUser: {prompt}",
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }).encode()

        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            result = json.loads(resp.read().decode())

        return LLMResponse(
            text=result.get("response", ""),
            model=self.config.model, provider="ollama",
            usage={'eval_count': result.get('eval_count', 0)},
        )

    def _call_gemini(self, prompt: str) -> LLMResponse:
        import urllib.request
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.model}:generateContent?key={self.config.api_key}"
        data = json.dumps({
            "contents": [{"parts": [{"text": f"{self.config.system_prompt}\n\n{prompt}"}]}],
            "generationConfig": {"temperature": self.config.temperature, "maxOutputTokens": self.config.max_tokens},
        }).encode()

        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            result = json.loads(resp.read().decode())

        text = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        return LLMResponse(text=text, model=self.config.model, provider="gemini")

    def _call_openai_compatible(self, prompt: str) -> LLMResponse:
        """For any OpenAI-compatible API (vLLM, LM Studio, text-generation-inference, etc.)."""
        import urllib.request
        url = f"{self.config.base_url}/chat/completions"
        data = json.dumps({
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.config.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }).encode()

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            result = json.loads(resp.read().decode())

        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return LLMResponse(text=text, model=self.config.model, provider="custom")

    @staticmethod
    def _default_model(provider: str) -> str:
        defaults = {
            'claude': 'claude-sonnet-4-6',
            'openai': 'gpt-4o',
            'ollama': 'llama3',
            'gemini': 'gemini-1.5-pro',
            'custom': 'default',
        }
        return defaults.get(provider, 'default')

    @staticmethod
    def available_providers() -> dict:
        return {
            'claude': {'models': ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5'], 'requires_key': True},
            'openai': {'models': ['gpt-4o', 'gpt-4-turbo', 'gpt-3.5-turbo', 'o1-preview'], 'requires_key': True},
            'ollama': {'models': ['llama3', 'llama3.1', 'mistral', 'mixtral', 'codellama', 'phi', 'gemma', 'deepseek-coder'], 'requires_key': False},
            'gemini': {'models': ['gemini-1.5-pro', 'gemini-pro'], 'requires_key': True},
            'custom': {'models': ['any'], 'requires_key': False, 'note': 'Any OpenAI-compatible endpoint'},
        }
