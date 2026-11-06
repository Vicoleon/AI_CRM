"""ADK tool and stream adapter for fixed, workspace-owned HTTP providers.

Only ADK's text and function-call subset is translated. A request containing
Google Search, media, schema-constrained output, or a provider-specific planner
is refused before network I/O. The allowlisted endpoint and credential are
resolved afresh for each model call in a tool loop.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from rubik_core.ai_providers.config import PROVIDER_ENDPOINTS
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, _key, _token_count
from rubik_core.tenancy.context import current_workspace

_REMOTE_PROVIDERS = frozenset({"anthropic", "openai", "deepseek", "meta"})
_MAX_RESPONSE_BYTES = 2_000_000
_SCHEMA_FIELDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "items",
        "enum",
        "anyOf",
        "allOf",
        "oneOf",
        "additionalProperties",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "default",
    }
)


def _schema(value: Any) -> dict[str, Any]:
    if isinstance(value, types.Schema):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(value, dict):
        raise IncompatibleModelRoute("ADK tool schema is unsupported")
    output: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _SCHEMA_FIELDS:
            continue
        if key == "type":
            if not isinstance(item, str):
                raise IncompatibleModelRoute("ADK tool schema is unsupported")
            output[key] = item.lower()
        elif key in {"properties"}:
            if not isinstance(item, dict):
                raise IncompatibleModelRoute("ADK tool schema is unsupported")
            output[key] = {name: _schema(sub) for name, sub in item.items()}
        elif key == "items":
            output[key] = _schema(item)
        elif key in {"anyOf", "allOf", "oneOf"}:
            if not isinstance(item, list):
                raise IncompatibleModelRoute("ADK tool schema is unsupported")
            output[key] = [_schema(sub) for sub in item]
        elif key == "additionalProperties" and isinstance(item, dict):
            output[key] = _schema(item)
        else:
            output[key] = item
    return output


def _system_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, types.Part):
        return value.text or ""
    if isinstance(value, types.Content):
        return "\n".join(part.text for part in value.parts or [] if part.text)
    if isinstance(value, list):
        return "\n".join(_system_text(part) for part in value)
    raise IncompatibleModelRoute("ADK system instruction is unsupported")


def _declarations(request: LlmRequest) -> list[types.FunctionDeclaration]:
    result: list[types.FunctionDeclaration] = []
    config = request.config
    if config.response_schema is not None or config.response_json_schema is not None:
        raise IncompatibleModelRoute("ADK response schema is unsupported")
    if config.thinking_config is not None or config.cached_content:
        raise IncompatibleModelRoute("ADK planner or cache is unsupported")
    if any(
        getattr(config, field) is not None
        for field in (
            "response_mime_type",
            "response_modalities",
            "speech_config",
            "image_config",
            "safety_settings",
            "routing_config",
            "model_selection_config",
            "audio_timestamp",
        )
    ):
        raise IncompatibleModelRoute("ADK generation option is unsupported")
    for tool in config.tools or []:
        if not isinstance(tool, types.Tool):
            raise IncompatibleModelRoute("ADK tool type is unsupported")
        fields = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        if set(fields) - {"functionDeclarations"}:
            raise IncompatibleModelRoute("ADK built-in tool is unsupported")
        result.extend(tool.function_declarations or [])
    names = [item.name for item in result]
    if any(not isinstance(name, str) or not name for name in names):
        raise IncompatibleModelRoute("ADK tool declaration is invalid")
    if len(names) != len(set(names)):
        raise IncompatibleModelRoute("ADK tool names must be unique")
    return result


def _tool_choice(request: LlmRequest, allowed: set[str], provider: str) -> Any | None:
    config = request.config.tool_config
    calling = config.function_calling_config if config is not None else None
    if calling is None or calling.mode is None:
        return None
    mode = calling.mode.value
    names = calling.allowed_function_names or []
    if any(name not in allowed for name in names):
        raise IncompatibleModelRoute("ADK tool choice is invalid")
    if mode == "NONE":
        return {"type": "none"} if provider == "anthropic" else "none"
    if mode in {"AUTO", "VALIDATED"}:
        return {"type": "auto"} if provider == "anthropic" else "auto"
    if mode != "ANY" or not allowed:
        raise IncompatibleModelRoute("ADK tool choice is unsupported")
    if provider == "anthropic":
        return (
            {"type": "tool", "name": names[0]} if len(names) == 1 else {"type": "any"}
        )
    return (
        {"type": "function", "function": {"name": names[0]}}
        if len(names) == 1
        else "required"
    )


def _parts(
    content: types.Content,
) -> tuple[list[str], list[types.FunctionCall], list[types.FunctionResponse]]:
    texts: list[str] = []
    calls: list[types.FunctionCall] = []
    results: list[types.FunctionResponse] = []
    for part in content.parts or []:
        if part.text is not None:
            texts.append(part.text)
        elif part.function_call is not None:
            calls.append(part.function_call)
        elif part.function_response is not None:
            results.append(part.function_response)
        else:
            raise IncompatibleModelRoute(
                "This workspace AI model cannot process attached media or special content"
            )
    return texts, calls, results


def _call_id(value: str | None) -> str:
    return value if isinstance(value, str) and value else f"call_{uuid.uuid4().hex}"


def _openai_messages(request: LlmRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = _system_text(request.config.system_instruction)
    if system:
        messages.append({"role": "system", "content": system})
    pending: dict[str, list[str]] = {}
    for content in request.contents:
        texts, calls, results = _parts(content)
        if calls:
            tool_calls = []
            for call in calls:
                if not call.name or not isinstance(call.args, dict):
                    raise IncompatibleModelRoute("ADK tool call is invalid")
                call_id = _call_id(call.id)
                pending.setdefault(call.name, []).append(call_id)
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.args),
                        },
                    }
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": "\n".join(texts) or None,
                    "tool_calls": tool_calls,
                }
            )
        elif texts:
            messages.append(
                {
                    "role": "assistant" if content.role == "model" else "user",
                    "content": "\n".join(texts),
                }
            )
        for result in results:
            if not result.name or not isinstance(result.response, dict):
                raise IncompatibleModelRoute("ADK tool response is invalid")
            ids = pending.get(result.name) or []
            call_id = result.id or (ids.pop(0) if ids else None)
            if not call_id:
                raise IncompatibleModelRoute("ADK tool response has no matching call")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result.response),
                }
            )
    return messages


def _anthropic_messages(request: LlmRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending: dict[str, list[str]] = {}
    for content in request.contents:
        texts, calls, results = _parts(content)
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": text} for text in texts if text
        ]
        for call in calls:
            if not call.name or not isinstance(call.args, dict):
                raise IncompatibleModelRoute("ADK tool call is invalid")
            call_id = _call_id(call.id)
            pending.setdefault(call.name, []).append(call_id)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": call.name,
                    "input": call.args,
                }
            )
        result_blocks: list[dict[str, Any]] = []
        for result in results:
            if not result.name or not isinstance(result.response, dict):
                raise IncompatibleModelRoute("ADK tool response is invalid")
            ids = pending.get(result.name) or []
            call_id = result.id or (ids.pop(0) if ids else None)
            if not call_id:
                raise IncompatibleModelRoute("ADK tool response has no matching call")
            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": json.dumps(result.response),
                }
            )
        # Anthropic requires all tool_result blocks to precede ordinary text
        # in a user message, including a turn that contains both.
        blocks = result_blocks + blocks
        if not blocks:
            continue
        role = "assistant" if content.role == "model" else "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    return messages


def _payload(
    route: ModelRoute, request: LlmRequest, stream: bool
) -> tuple[dict[str, Any], dict[str, str], set[str]]:
    declarations = _declarations(request)
    calling = (
        request.config.tool_config.function_calling_config
        if request.config.tool_config is not None
        else None
    )
    allowed_names = set(calling.allowed_function_names or []) if calling else set()
    if allowed_names:
        declared_names = {item.name for item in declarations}
        if not allowed_names <= declared_names:
            raise IncompatibleModelRoute("ADK tool choice is invalid")
        declarations = [item for item in declarations if item.name in allowed_names]
    names = {item.name for item in declarations}
    choice = _tool_choice(request, names, route.provider)
    max_tokens = request.config.max_output_tokens or 4096
    if route.provider == "anthropic":
        payload: dict[str, Any] = {
            "model": route.model,
            "max_tokens": max_tokens,
            "messages": _anthropic_messages(request),
            "stream": stream,
        }
        system = _system_text(request.config.system_instruction)
        if system:
            payload["system"] = system
        if declarations:
            payload["tools"] = [
                {
                    "name": item.name,
                    "description": item.description or "",
                    "input_schema": _schema(
                        item.parameters_json_schema
                        or item.parameters
                        or {"type": "object", "properties": {}}
                    ),
                }
                for item in declarations
            ]
        if choice is not None:
            payload["tool_choice"] = choice
        headers = {
            "x-api-key": _key(route),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        payload = {
            "model": route.model,
            "messages": _openai_messages(request),
            "stream": stream,
        }
        if route.provider == "meta" and payload["messages"]:
            if payload["messages"][0]["role"] == "system":
                payload["messages"][0]["role"] = "developer"
        if route.provider == "openai":
            payload["max_completion_tokens"] = max_tokens
            # ADK function calls need the model's non-reasoning Chat
            # Completions mode, including connection probes without temperature.
            payload["reasoning_effort"] = "none"
        else:
            payload["max_tokens"] = max_tokens
            if route.provider == "deepseek":
                # Thinking mode requires reasoning_content replay during tool
                # loops and rejects forced tool_choice. ADK's Content cannot
                # round-trip that provider-specific field.
                payload["thinking"] = {"type": "disabled"}
        if declarations:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description or "",
                        "parameters": _schema(
                            item.parameters_json_schema
                            or item.parameters
                            or {"type": "object", "properties": {}}
                        ),
                    },
                }
                for item in declarations
            ]
        if choice is not None:
            payload["tool_choice"] = choice
        headers = {
            "Authorization": f"Bearer {_key(route)}",
            "Content-Type": "application/json",
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
    if request.config.temperature is not None:
        payload["temperature"] = request.config.temperature
    return payload, headers, names


def _usage(prompt: Any, completion: Any) -> types.GenerateContentResponseUsageMetadata:
    input_count = _token_count(prompt)
    output_count = _token_count(completion)
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=input_count,
        candidates_token_count=output_count,
        total_token_count=input_count + output_count,
    )


def _tool_call(
    name: Any, arguments: Any, call_id: Any, allowed: set[str]
) -> types.Part:
    if not isinstance(name, str) or name not in allowed:
        raise ProviderRequestError("AI provider returned an unknown tool")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            raise ProviderRequestError(
                "AI provider tool arguments were invalid"
            ) from None
    if not isinstance(arguments, dict):
        raise ProviderRequestError("AI provider tool arguments were invalid")
    if not isinstance(call_id, str) or not call_id:
        raise ProviderRequestError("AI provider tool call id was invalid")
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=arguments)
    )


def _openai_response(data: Any, model: str, allowed: set[str]) -> LlmResponse:
    if not isinstance(data, dict):
        raise ProviderRequestError("AI provider response was invalid")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderRequestError("AI provider response was invalid")
    finish_reason = choices[0].get("finish_reason")
    if finish_reason is not None and finish_reason not in ("stop", "tool_calls"):
        raise ProviderRequestError("AI provider answer ended before completion")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderRequestError("AI provider response was invalid")
    parts = []
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(types.Part(text=content))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderRequestError("AI provider tool call was invalid")
        function = call.get("function")
        if not isinstance(function, dict):
            raise ProviderRequestError("AI provider tool call was invalid")
        parts.append(
            _tool_call(
                function.get("name"), function.get("arguments"), call.get("id"), allowed
            )
        )
    if not parts:
        raise ProviderRequestError("AI provider returned no answer")
    raw_usage = data.get("usage") or {}
    if not isinstance(raw_usage, dict):
        raise ProviderRequestError("AI provider usage was invalid")
    return LlmResponse(
        model_version=model,
        content=types.Content(role="model", parts=parts),
        usage_metadata=_usage(
            raw_usage.get("prompt_tokens"), raw_usage.get("completion_tokens")
        ),
    )


def _anthropic_response(data: Any, model: str, allowed: set[str]) -> LlmResponse:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ProviderRequestError("AI provider response was invalid")
    stop_reason = data.get("stop_reason")
    if stop_reason is not None and stop_reason not in (
        "end_turn",
        "tool_use",
        "stop_sequence",
    ):
        raise ProviderRequestError("AI provider answer ended before completion")
    parts = []
    for block in data["content"]:
        if not isinstance(block, dict):
            raise ProviderRequestError("AI provider response was invalid")
        if (
            block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"]
        ):
            parts.append(types.Part(text=block["text"]))
        elif block.get("type") == "tool_use":
            parts.append(
                _tool_call(
                    block.get("name"), block.get("input"), block.get("id"), allowed
                )
            )
    if not parts:
        raise ProviderRequestError("AI provider returned no answer")
    raw_usage = data.get("usage") or {}
    if not isinstance(raw_usage, dict):
        raise ProviderRequestError("AI provider usage was invalid")
    return LlmResponse(
        model_version=model,
        content=types.Content(role="model", parts=parts),
        usage_metadata=_usage(
            raw_usage.get("input_tokens"), raw_usage.get("output_tokens")
        ),
    )


def _response(route: ModelRoute, data: Any, allowed: set[str]) -> LlmResponse:
    if route.provider == "anthropic":
        return _anthropic_response(data, route.model, allowed)
    return _openai_response(data, route.model, allowed)


async def _stream_openai(
    response: httpx.Response, model: str, allowed: set[str]
) -> AsyncGenerator[LlmResponse, None]:
    text_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}
    prompt_tokens = completion_tokens = 0
    size = 0
    completed = False
    finish_reason = None
    async for line in response.aiter_lines():
        size += len(line)
        if size > _MAX_RESPONSE_BYTES:
            raise ProviderRequestError("AI provider response was too large")
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            completed = True
            break
        try:
            event = json.loads(raw)
        except ValueError:
            raise ProviderRequestError("AI provider stream was invalid") from None
        if not isinstance(event, dict):
            raise ProviderRequestError("AI provider stream was invalid")
        usage = event.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = _token_count(usage.get("prompt_tokens"))
            completion_tokens = _token_count(usage.get("completion_tokens"))
        choices = event.get("choices") or []
        if not isinstance(choices, list):
            raise ProviderRequestError("AI provider stream was invalid")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ProviderRequestError("AI provider stream was invalid")
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ProviderRequestError("AI provider stream was invalid")
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                text_parts.append(piece)
                yield LlmResponse(
                    model_version=model,
                    content=types.Content(role="model", parts=[types.Part(text=piece)]),
                    partial=True,
                )
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                    raise ProviderRequestError(
                        "AI provider stream tool call was invalid"
                    )
                item = tool_parts.setdefault(
                    call["index"], {"id": "", "name": "", "arguments": ""}
                )
                if isinstance(call.get("id"), str):
                    item["id"] += call["id"]
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    raise ProviderRequestError(
                        "AI provider stream tool call was invalid"
                    )
                for field in ("name", "arguments"):
                    if isinstance(function.get(field), str):
                        item[field] += function[field]
    if not completed:
        raise ProviderRequestError("AI provider stream ended before completion")
    if finish_reason is not None and finish_reason not in ("stop", "tool_calls"):
        raise ProviderRequestError("AI provider answer ended before completion")
    parts: list[types.Part] = []
    if text_parts:
        parts.append(types.Part(text="".join(text_parts)))
    for index in sorted(tool_parts):
        call = tool_parts[index]
        parts.append(_tool_call(call["name"], call["arguments"], call["id"], allowed))
    if not parts:
        raise ProviderRequestError("AI provider returned no answer")
    yield LlmResponse(
        model_version=model,
        content=types.Content(role="model", parts=parts),
        partial=False,
        usage_metadata=_usage(prompt_tokens, completion_tokens),
    )


async def _stream_anthropic(
    response: httpx.Response, model: str, allowed: set[str]
) -> AsyncGenerator[LlmResponse, None]:
    text_parts: list[str] = []
    blocks: dict[int, dict[str, Any]] = {}
    prompt_tokens = completion_tokens = 0
    size = 0
    completed = False
    stop_reason = None
    async for line in response.aiter_lines():
        size += len(line)
        if size > _MAX_RESPONSE_BYTES:
            raise ProviderRequestError("AI provider response was too large")
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            raise ProviderRequestError("AI provider stream was invalid") from None
        if not isinstance(event, dict):
            raise ProviderRequestError("AI provider stream was invalid")
        kind = event.get("type")
        if kind == "error":
            raise ProviderRequestError("AI provider stream failed")
        if kind == "message_stop":
            completed = True
            break
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            if isinstance(usage, dict):
                prompt_tokens = _token_count(usage.get("input_tokens"))
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                stop_reason = delta["stop_reason"]
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                completion_tokens = _token_count(usage.get("output_tokens"))
        elif kind == "content_block_start":
            index = event.get("index")
            if not isinstance(index, int) or not isinstance(
                event.get("content_block"), dict
            ):
                raise ProviderRequestError("AI provider stream was invalid")
            blocks[index] = dict(event["content_block"])
            initial = (
                blocks[index].get("text")
                if blocks[index].get("type") == "text"
                else None
            )
            if isinstance(initial, str) and initial:
                text_parts.append(initial)
                yield LlmResponse(
                    model_version=model,
                    content=types.Content(
                        role="model", parts=[types.Part(text=initial)]
                    ),
                    partial=True,
                )
        elif kind == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if (
                not isinstance(index, int)
                or not isinstance(delta, dict)
                or index not in blocks
            ):
                raise ProviderRequestError("AI provider stream was invalid")
            piece = delta.get("text") if delta.get("type") == "text_delta" else None
            if isinstance(piece, str) and piece:
                text_parts.append(piece)
                yield LlmResponse(
                    model_version=model,
                    content=types.Content(role="model", parts=[types.Part(text=piece)]),
                    partial=True,
                )
            args = (
                delta.get("partial_json")
                if delta.get("type") == "input_json_delta"
                else None
            )
            if isinstance(args, str):
                blocks[index]["partial_json"] = (
                    blocks[index].get("partial_json", "") + args
                )
    if not completed:
        raise ProviderRequestError("AI provider stream ended before completion")
    if stop_reason is not None and stop_reason not in (
        "end_turn",
        "tool_use",
        "stop_sequence",
    ):
        raise ProviderRequestError("AI provider answer ended before completion")
    parts: list[types.Part] = []
    if text_parts:
        parts.append(types.Part(text="".join(text_parts)))
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "tool_use":
            args = block.get("partial_json") or block.get("input") or {}
            parts.append(_tool_call(block.get("name"), args, block.get("id"), allowed))
    if not parts:
        raise ProviderRequestError("AI provider returned no answer")
    yield LlmResponse(
        model_version=model,
        content=types.Content(role="model", parts=parts),
        partial=False,
        usage_metadata=_usage(prompt_tokens, completion_tokens),
    )


class WorkspaceRemoteLlm(BaseLlm):
    """One ADK invocation routed to a fixed provider endpoint."""

    _route: ModelRoute = PrivateAttr()

    @classmethod
    def for_route(cls, route: ModelRoute) -> WorkspaceRemoteLlm:
        if route.provider not in _REMOTE_PROVIDERS:
            raise IncompatibleModelRoute("Provider has no remote ADK adapter")
        model = cls(model=route.model)
        model._route = route
        return model

    @property
    def route(self) -> ModelRoute:
        return self._route

    @classmethod
    def supported_models(cls) -> list[str]:
        # The provider catalog, not ADK's model-name auto match, is the gate.
        return []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        route = self._route
        if current_workspace() != route.workspace:
            raise IncompatibleModelRoute("Workspace AI route changed during this turn")
        capability = "tools_stream" if route.feature == "chat_adk" else "tools"
        if (
            resolve_model_route(route.feature, capability, default_model=route.model)
            != route
        ):
            raise IncompatibleModelRoute("Workspace AI route changed during this turn")
        if stream and capability != "tools_stream":
            # Other ADK jobs may ask ADK to stream even though their Admin route
            # probes only tool calls. They are not allowed to silently upgrade.
            raise IncompatibleModelRoute("Selected model has not passed a stream probe")
        async for item in _invoke_remote(route, llm_request, stream):
            yield item


async def _invoke_remote(
    route: ModelRoute, llm_request: LlmRequest, stream: bool
) -> AsyncGenerator[LlmResponse, None]:
    """Shared production/probe transport; caller owns route authorization."""
    payload, headers, names = _payload(route, llm_request, stream)
    url = PROVIDER_ENDPOINTS[route.provider]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if stream:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if response.status_code != 200:
                        raise ProviderRequestError(
                            f"AI provider returned HTTP {response.status_code}"
                        )
                    parser = (
                        _stream_anthropic
                        if route.provider == "anthropic"
                        else _stream_openai
                    )
                    async for item in parser(response, route.model, names):
                        yield item
            else:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code != 200:
                    raise ProviderRequestError(
                        f"AI provider returned HTTP {response.status_code}"
                    )
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise ProviderRequestError("AI provider response was too large")
                try:
                    data = response.json()
                except ValueError:
                    raise ProviderRequestError(
                        "AI provider response was invalid"
                    ) from None
                yield _response(route, data, names)
    except (ProviderRequestError, IncompatibleModelRoute):
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ProviderRequestError("AI provider request failed") from None


async def test_remote_adk_connection(
    route: ModelRoute, *, stream: bool = False
) -> None:
    """Probe a real function-call and function-result roundtrip.

    This runs before a route is active, so it uses the production transport
    without the active-route check. A plain text response is insufficient:
    the provider must return a well-formed call and then consume its result.
    """
    if route.provider not in _REMOTE_PROVIDERS:
        raise IncompatibleModelRoute("Provider has no remote ADK adapter")
    if current_workspace() != route.workspace:
        raise IncompatibleModelRoute("Workspace AI route changed during test")
    nonce = uuid.uuid4().hex[:12]
    declaration = types.FunctionDeclaration(
        name="cubie_connection_probe",
        description="Echo a nonce to test tool calling",
        parameters=types.Schema(
            type="OBJECT",
            properties={"nonce": types.Schema(type="STRING")},
            required=["nonce"],
        ),
    )
    user = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Call cubie_connection_probe with nonce "
                    f"{nonce}. After the tool result, answer in one word."
                )
            )
        ],
    )
    first = LlmRequest(
        model=route.model,
        contents=[user],
        config=types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=[declaration])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["cubie_connection_probe"],
                )
            ),
        ),
    )
    first_responses = [
        item async for item in _invoke_remote(route, first, stream) if not item.partial
    ]
    if not first_responses or first_responses[-1].content is None:
        raise ProviderRequestError("AI provider did not complete the tool probe")
    calls = [
        part.function_call
        for part in first_responses[-1].content.parts or []
        if part.function_call is not None
    ]
    if (
        len(calls) != 1
        or calls[0].name != "cubie_connection_probe"
        or calls[0].args != {"nonce": nonce}
        or not calls[0].id
    ):
        raise ProviderRequestError("AI provider did not pass the tool probe")
    second = LlmRequest(
        model=route.model,
        contents=[
            user,
            first_responses[-1].content,
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=calls[0].id,
                            name="cubie_connection_probe",
                            response={"nonce": nonce, "ok": True},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=[declaration])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE")
            ),
        ),
    )
    second_responses = [
        item async for item in _invoke_remote(route, second, stream) if not item.partial
    ]
    if (
        not second_responses
        or second_responses[-1].content is None
        or not any(
            isinstance(part.text, str) and part.text.strip()
            for part in second_responses[-1].content.parts or []
        )
    ):
        raise ProviderRequestError("AI provider did not complete the tool probe")
