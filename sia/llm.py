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
DEFAULT_MODELS = {"gemini": "gemini-flash-latest", "anthropic": "claude-haiku-5-5"}


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, provider="gemini", api_key="", model=None, base_url=None):
        provider = (provider or "gemini").lower()
        if provider not in PROVIDERS:
            raise LLMError(f"Unknown provider '{provider}'. Choose one of: {', '.join(sorted(PROVIDERS))}")
        # A caller-chosen base URL lets a hosted server be pointed at internal hosts (SSRF), so it is opt-in.
        if base_url and os.getenv("SIA_ALLOW_CUSTOM_BASE_URL") != "1":
            raise LLMError("Custom base URLs are disabled on this server (set SIA_ALLOW_CUSTOM_BASE_URL=1).")
        if provider == "ollama" and not base_url and os.getenv("SIA_ALLOW_CUSTOM_BASE_URL") != "1":
            raise LLMError("Ollama (localhost) is only available on self-hosted servers.")
        if not api_key and provider != "ollama":
            raise LLMError("Missing LLM API key.")
        self.model = model or DEFAULT_MODELS.get(provider)
        if not self.model:
            raise LLMError(f"Provider '{provider}' needs an explicit model name.")
        self.provider, self.api_key = provider, api_key
        self.base_url = (base_url or OPENAI_COMPATIBLE.get(provider, "")).rstrip("/")
        self.calls = 0
        self.http = httpx.Client(timeout=120)

    def text(self, system, prompt, temperature=0.7, as_json=False):
        self.calls += 1
        if self.provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
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
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))

        if self.provider == "anthropic":
            data = self._post("https://api.anthropic.com/v1/messages",
                              {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                              {"model": self.model, "max_tokens": 4096, "temperature": temperature,
                               "system": system, "messages": [{"role": "user", "content": prompt}]})
            return "".join(b.get("text", "") for b in data.get("content", []))

        data = self._post(f"{self.base_url}/chat/completions",
                          {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                          {"model": self.model, "temperature": temperature,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]})
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise LLMError(f"Provider returned no content: {str(data)[:300]}")

    def json(self, system, prompt, temperature=0.2):
        raw = self.text(system + "\nRespond with a single JSON object only.", prompt, temperature, as_json=True)
        return parse_json(raw)

    def _post(self, url, headers, body, tries=7):
        for attempt in range(tries):
            try:
                r = self.http.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                if attempt == tries - 1:
                    raise LLMError(f"Network error calling {self.provider}: {e}")
                time.sleep(2 ** attempt)
                continue
            # Free tiers rate-limit hard; back off and retry instead of failing the task.
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                retry_after = r.headers.get("retry-after", "")
                time.sleep(min(float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1), 60))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.provider} error {r.status_code}: {r.text[:500]}")
            return r.json()


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
