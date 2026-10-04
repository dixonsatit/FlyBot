"""LLM providers for the slow "cortex" layer: Claude (Anthropic SDK) or any
OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, LM Studio, Typhoon, OpenRouter, ...).

Both run the same small tool loop: send the conversation with tool definitions,
execute requested tools locally, feed results back, stop at a final answer or
after ``max_steps``. Conversation memory between calls is kept as plain
user/assistant text so either provider can continue it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
# models that accept the server-side refusal fallback ("default" form) on the Claude API
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema object
    fn: Callable[..., Any]

    def call(self, args: dict) -> str:
        try:
            result = self.fn(**args)
        except Exception as e:  # report tool failures to the model instead of crashing
            log.warning("tool %s failed: %s", self.name, e)
            return json.dumps({"error": str(e)})
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class LLM:
    """Common interface: ``ask(system, history, text, tools, image_jpeg) -> reply text``."""

    supports_images = True

    def ask(self, system: str, history: list[dict], text: str, tools: list[Tool] | None = None,
            image_jpeg: bytes | None = None, max_steps: int = 4) -> str:
        raise NotImplementedError


class AnthropicLLM(LLM):
    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: str | None = None,
                 base_url: str | None = None, effort: str = "low", max_tokens: int = 2048,
                 fallbacks: bool = True, timeout: float = 30.0):
        import anthropic

        kwargs = {"timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # server-side refusal fallback: Claude API only, current models only
        self.fallbacks = fallbacks and model in _FALLBACK_MODELS and not base_url

    def _create(self, **params):
        params.update(model=self.model, max_tokens=self.max_tokens, output_config={"effort": self.effort})
        if self.fallbacks:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default", **params)
        return self.client.messages.create(**params)

    def ask(self, system, history, text, tools=None, image_jpeg=None, max_steps=4):
        content: list[dict] = []
        if image_jpeg:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.standard_b64encode(image_jpeg).decode()}})
        content.append({"type": "text", "text": text})
        messages = [*history, {"role": "user", "content": content}]
        tool_map = {t.name: t for t in tools or []}
        params = {"system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in tools]
        for _ in range(max_steps):
            response = self._create(messages=messages, **params)
            if response.stop_reason == "refusal":
                log.warning("LLM refused (%s)", getattr(response.stop_details, "category", None))
                return ""
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                return "".join(b.text for b in response.content if b.type == "text").strip()
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": c.id,
                 **({"content": tool_map[c.name].call(dict(c.input))} if c.name in tool_map
                    else {"content": f"unknown tool {c.name}", "is_error": True})}
                for c in calls
            ]})
        log.warning("LLM tool loop stopped after %d steps", max_steps)
        return ""


class OpenAICompatLLM(LLM):
    """Chat Completions API: api.openai.com or any compatible server (set ``base_url``)."""

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 max_tokens: int = 4096, supports_images: bool = True, timeout: float = 120.0,
                 extra_body: dict | None = None):
        import openai

        # local servers (Ollama, vLLM, LM Studio) accept any key
        key = api_key or os.environ.get("OPENAI_API_KEY") or ("not-needed" if base_url else None)
        self.client = openai.OpenAI(api_key=key, base_url=base_url, timeout=timeout)
        self.model = model
        self.max_tokens = max_tokens
        self.supports_images = supports_images
        # server-specific options, e.g. {"chat_template_kwargs": {"enable_thinking": false}} for
        # Qwen3 on vLLM: reasoning models otherwise spend the token budget thinking
        self.extra_body = extra_body or {}

    def ask(self, system, history, text, tools=None, image_jpeg=None, max_steps=4):
        if image_jpeg and self.supports_images:
            url = "data:image/jpeg;base64," + base64.standard_b64encode(image_jpeg).decode()
            user = {"role": "user", "content": [{"type": "text", "text": text},
                                                {"type": "image_url", "image_url": {"url": url}}]}
        else:
            user = {"role": "user", "content": text}
        messages = [{"role": "system", "content": system}, *history, user]
        tool_map = {t.name: t for t in tools or []}
        params = {}
        if tools:
            params["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
        for _ in range(max_steps):
            response = self.client.chat.completions.create(model=self.model, messages=messages,
                                                           max_tokens=self.max_tokens, extra_body=self.extra_body,
                                                           **params)
            choice = response.choices[0]
            msg = choice.message
            if choice.finish_reason == "length" and not msg.content and not msg.tool_calls:
                log.warning("LLM used all %d tokens without answering (reasoning?); raise --llm-max-tokens "
                            "or turn thinking off via --llm-extra-body", self.max_tokens)
            calls = msg.tool_calls or []
            if not calls:
                return (msg.content or "").strip()
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name,
                                                              "arguments": c.function.arguments}}
                for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    result = json.dumps({"error": "arguments were not valid JSON"})
                else:
                    tool = tool_map.get(c.function.name)
                    result = tool.call(args) if tool else json.dumps({"error": f"unknown tool {c.function.name}"})
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
        log.warning("LLM tool loop stopped after %d steps", max_steps)
        return ""


def make_llm(provider: str, model: str | None = None, base_url: str | None = None,
             api_key: str | None = None, vision: bool = True, max_tokens: int | None = None,
             extra_body: dict | None = None) -> LLM | None:
    """Build a provider from CLI options; ``none`` disables the cortex layer."""
    if provider == "none":
        return None
    if provider == "anthropic":
        return AnthropicLLM(model or DEFAULT_ANTHROPIC_MODEL, api_key=api_key, base_url=base_url,
                            **({"max_tokens": max_tokens} if max_tokens else {}))
    if provider == "openai":
        if not model:
            raise ValueError("--llm-model is required for an OpenAI-compatible provider")
        return OpenAICompatLLM(model, base_url=base_url, api_key=api_key, supports_images=vision,
                               extra_body=extra_body, **({"max_tokens": max_tokens} if max_tokens else {}))
    raise ValueError(f"unknown LLM provider {provider!r}")
