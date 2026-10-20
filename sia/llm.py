"""Bring-your-own-key LLM client. Raw HTTP so every provider is ~10 lines and no SDKs are needed."""
import json
import os
import re
import time

import httpx

# Known OpenAI-compatible endpoints. Many have free tiers (Groq, OpenRouter ":free" models, Ollama locally).
OPENAI_COMPATIBLE = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
}
PROVIDERS = {"gemini", "anthropic", *OPENAI_COMPATIBLE}
# Comma-separated = fallback chain: free models get overloaded (503), so try the next one.
DEFAULT_MODELS = {"gemini": "gemini-flash-latest,gemini-flash-lite-latest", "anthropic": "claude-haiku-5-5"}
# model names go into request URLs (Gemini) and come from a request header, so only plain names are accepted
MODEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
MAX_FALLBACKS = 4


class LLMError(Exception):
    pass


class Unavailable(LLMError):
    """Overloaded/rate-limited/unreachable after retries; worth trying the next model in the chain."""


class LLM:
    def __init__(self, provider="gemini", api_key="", model=None, base_url=None):
        provider = (provider or "gemini").lower()
        if provider not in PROVIDERS:
            raise LLMError(f"Unknown provider '{provider}'. Choose one of: {', '.join(sorted(PROVIDERS))}")
        # A caller-chosen base URL lets a hosted server be pointed at internal hosts (SSRF), so it is opt-in.
        if base_url and os.getenv("SIA_ALLOW_CUSTOM_BASE_URL") != "1":
            raise LLMError("Custom base URLs are disabled on this server (set SIA_ALLOW_CUSTOM_BASE_URL=1).")
        if base_url and not re.match(r"^https?://[^\s/?#]+(/[^\s?#]*)?$", base_url):
            raise LLMError("X-LLM-Base-URL must be an http(s) URL without a query string.")
        if provider == "ollama" and not base_url and os.getenv("SIA_ALLOW_CUSTOM_BASE_URL") != "1":
            raise LLMError("Ollama (localhost) is only available on self-hosted servers.")
        if not api_key and provider != "ollama":
            raise LLMError("Missing LLM API key.")
        self.models = [m.strip() for m in (model or DEFAULT_MODELS.get(provider, "")).split(",") if m.strip()]
        if not self.models:
            raise LLMError(f"Provider '{provider}' needs an explicit model name.")
        if len(self.models) > MAX_FALLBACKS or any(not MODEL_RX.match(m) or ".." in m for m in self.models):
            raise LLMError(f"Invalid model name (letters, digits and . _ : / @ -; at most {MAX_FALLBACKS} "
                           "comma-separated fallbacks).")
        self.model = self.models[0]
        self.tries = 7 if len(self.models) == 1 else 3  # with fallbacks, move on sooner
        self.provider, self.api_key = provider, api_key
        self.base_url = (base_url or OPENAI_COMPATIBLE.get(provider, "")).rstrip("/")
        self.calls = 0
        self.last_usage = {}  # {"model", "prompt_tokens", "completion_tokens"} of the latest successful call
        self.http = httpx.Client(timeout=httpx.Timeout(90, connect=10))  # no redirects: they could reach elsewhere

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        for i, model in enumerate(self.models):
            try:
                return self._text(model, system, prompt, temperature, as_json)
            except Unavailable:
                if i == len(self.models) - 1:
                    raise

    def _text(self, model, system, prompt, temperature, as_json):
        if self.provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            gen = {"temperature": temperature}
            if as_json:
                gen["responseMimeType"] = "application/json"
            data = self._post(url, {"x-goog-api-key": self.api_key}, {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen,
            })
            try:
                parts = data["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError):
                raise LLMError(f"Gemini returned no content: {str(data)[:300]}")
            u = data.get("usageMetadata") or {}
            self._usage(model, u.get("promptTokenCount"), u.get("candidatesTokenCount"))
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))

        if self.provider == "anthropic":
            data = self._post("https://api.anthropic.com/v1/messages",
                              {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                              {"model": model, "max_tokens": 4096, "temperature": temperature,
                               "system": system, "messages": [{"role": "user", "content": prompt}]})
            u = data.get("usage") or {}
            self._usage(model, u.get("input_tokens"), u.get("output_tokens"))
            return "".join(b.get("text", "") for b in data.get("content", []))

        data = self._post(f"{self.base_url}/chat/completions",
                          {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                          {"model": model, "temperature": temperature,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]})
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise LLMError(f"Provider returned no content: {str(data)[:300]}")
        u = data.get("usage") or {}
        self._usage(model, u.get("prompt_tokens"), u.get("completion_tokens"))
        return content

    def _usage(self, model, prompt_tokens, completion_tokens):
        self.last_usage = {"model": model, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}

    def json(self, system, prompt, temperature=0.2):
        raw = self.text(system + "\nRespond with a single JSON object only.", prompt, temperature, as_json=True)
        return parse_json(raw)

    def _post(self, url, headers, body):
        tries = self.tries
        for attempt in range(tries):
            try:
                r = self.http.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                if attempt == tries - 1:
                    raise Unavailable(self._clean(f"Network error calling {self.provider}: {e}"))
                time.sleep(2 ** attempt)
                continue
            # Free tiers rate-limit hard; back off and retry instead of failing the task.
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                retry_after = r.headers.get("retry-after", "")
                time.sleep(min(float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1), 60))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                raise Unavailable(self._clean(f"{self.provider} error {r.status_code}: {r.text[:300]}"))
            if r.status_code >= 400:
                raise LLMError(self._clean(f"{self.provider} error {r.status_code}: {r.text[:500]}"))
            return r.json()

    def _clean(self, text):
        """Provider errors are returned to the caller; never let them carry the key back out (or into logs)."""
        return text.replace(self.api_key, "***") if self.api_key else text


def parse_json(raw):
    """Parse a JSON object out of model output, tolerating code fences and chatter around it."""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"Model did not return valid JSON: {raw[:300]}")
