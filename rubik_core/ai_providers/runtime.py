"""Server-only inference adapters for workspace-owned credentials.

Each model call requests its actual capability. Native Google media and
grounding, remote ADK tools and streaming, and separate embedding adapters
fail closed when a provider cannot run the selected workload.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import wave
from dataclasses import dataclass
from typing import Any

import requests
from google import genai
from google.genai import types

from rubik_core.ai_providers.config import PROVIDER_ENDPOINTS
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    provider_connection_route,
    resolve_model_route,
)
from rubik_core.tenancy.secrets import workspace_secret


@dataclass(frozen=True)
class ModelTextResponse:
    text: str
    provider: str
    model: str
    billing_owner: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    citation_urls: tuple[str, ...] = ()


class ProviderRequestError(RuntimeError):
    """A selected client provider failed; callers must surface this result."""


def _token_count(value: Any) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        raise ProviderRequestError("AI provider usage was invalid") from None
    if count < 0 or count > 10_000_000_000:
        raise ProviderRequestError("AI provider usage was invalid")
    return count


def _temperature(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Temperature must be a number between 0 and 1")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("Temperature must be a number between 0 and 1") from None
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("Temperature must be a number between 0 and 1")
    return result


def _record(
    route: ModelRoute,
    prompt: int,
    completion: int,
    *,
    thinking: int = 0,
    cached: int = 0,
) -> None:
    from rubik_core.cost.usage import record_usage

    record_usage(
        operation="generate",
        model=route.model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        thinking_tokens=thinking,
        cached_tokens=cached,
        metadata={"provider": route.provider, "billing_owner": route.billing_owner},
    )


def _genai_client(route: ModelRoute, key: str | None):
    if route.provider == "vertex_managed":
        from rubik_core.models import get_genai_client

        return get_genai_client()
    if route.provider == "gemini":
        return genai.Client(
            vertexai=False,
            api_key=key,
            http_options=types.HttpOptions(timeout=30_000),
        )
    if route.provider == "vertex_client":
        # Impersonation uses the running workload identity. The browser never
        # uploads a service-account private key.
        import google.auth
        from google.auth import impersonated_credentials

        source, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=route.service_identity,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=3600,
        )
        return genai.Client(
            vertexai=True,
            project=route.project,
            location=route.region,
            credentials=credentials,
            http_options=types.HttpOptions(timeout=30_000),
        )
    raise IncompatibleModelRoute("Provider does not have a native Gemini adapter")


def _key(route: ModelRoute) -> str | None:
    if route.provider in ("vertex_managed", "vertex_client"):
        return None
    key = workspace_secret(f"ai_{route.provider}", "api_key")
    if not key:
        raise IncompatibleModelRoute(f"{route.provider} API key is unavailable")
    return key


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict:
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise ProviderRequestError("AI provider request failed") from None
    if response.status_code != 200:
        # Provider bodies can contain customer inputs, credentials or internal
        # details. Surface only the status class, never their raw text.
        raise ProviderRequestError(f"AI provider returned HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError:
        raise ProviderRequestError("AI provider response was invalid") from None
    if not isinstance(value, dict):
        raise ProviderRequestError("AI provider response was invalid")
    return value


def _remote_text(
    route: ModelRoute,
    key: str,
    prompt: str,
    *,
    json_mode: bool,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> ModelTextResponse:
    temperature = _temperature(temperature)
    if (
        temperature is not None
        and route.provider == "openai"
        and route.model != "gpt-6-luna"
    ):
        raise IncompatibleModelRoute(
            "Temperature is not verified for this OpenAI model"
        )
    url = PROVIDER_ENDPOINTS[route.provider]
    if route.provider == "anthropic":
        payload = {
            "model": route.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_instruction:
            payload["system"] = system_instruction
        if temperature is not None:
            payload["temperature"] = temperature
        data = _post_json(
            url,
            {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload,
        )
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderRequestError("AI provider response was invalid")
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise ProviderRequestError("AI provider response was invalid")
        cached_tokens = _token_count(usage.get("cache_read_input_tokens"))
        prompt_tokens = (
            _token_count(usage.get("input_tokens"))
            + cached_tokens
            + _token_count(usage.get("cache_creation_input_tokens"))
        )
        completion_tokens = _token_count(usage.get("output_tokens"))
    else:
        messages = [{"role": "user", "content": prompt}]
        if system_instruction:
            # Meta prefers developer; OpenAI-compatible system messages remain
            # the verified common instruction role for OpenAI and DeepSeek.
            role = "developer" if route.provider == "meta" else "system"
            messages.insert(0, {"role": role, "content": system_instruction})
        payload = {
            "model": route.model,
            "messages": messages,
        }
        if route.provider == "openai":
            payload["max_completion_tokens"] = 4096
            if temperature is not None:
                # The allowlisted GPT-6 Luna accepts custom sampling in Chat
                # Completions only with reasoning disabled. This trades model
                # reasoning for the caller's requested sampling setting.
                payload["reasoning_effort"] = "none"
        else:
            payload["max_tokens"] = 4096
        if temperature is not None:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(
            url,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload,
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderRequestError("AI provider response was invalid")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise ProviderRequestError("AI provider response was invalid")
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        completion_tokens = _token_count(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            raise ProviderRequestError("AI provider usage was invalid")
        cached_tokens = (
            _token_count(details.get("cached_tokens"))
            if route.provider == "openai"
            else _token_count(usage.get("prompt_cache_hit_tokens"))
            if route.provider == "deepseek"
            else 0
        )
    if not isinstance(text, str) or not text:
        raise ProviderRequestError("AI provider returned no text")
    _record(route, prompt_tokens, completion_tokens, cached=cached_tokens)
    return ModelTextResponse(
        text,
        route.provider,
        route.model,
        route.billing_owner,
        prompt_tokens,
        completion_tokens,
    )


def generate_text(
    feature: str,
    prompt: str,
    *,
    default_model: str,
    json_mode: bool = False,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> ModelTextResponse:
    """Generate one plain-text answer with no cross-provider fallback."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Prompt must be nonempty text")
    if system_instruction is not None and not isinstance(system_instruction, str):
        raise ValueError("System instruction must be text")
    temperature = _temperature(temperature)
    route = resolve_model_route(
        feature, "json" if json_mode else "text", default_model=default_model
    )
    return _generate_with_route(
        route,
        prompt,
        json_mode=json_mode,
        system_instruction=system_instruction,
        temperature=temperature,
    )


def generate_text_on_route(
    route: ModelRoute,
    prompt: str,
    *,
    json_mode: bool = False,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> ModelTextResponse:
    """Use an invocation's pinned route, refusing a changed or revoked route."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Prompt must be nonempty text")
    if system_instruction is not None and not isinstance(system_instruction, str):
        raise ValueError("System instruction must be text")
    temperature = _temperature(temperature)
    live = resolve_model_route(
        route.feature, "json" if json_mode else "text", default_model=route.model
    )
    if live != route:
        raise IncompatibleModelRoute("Workspace AI route changed during this request")
    return _generate_with_route(
        route,
        prompt,
        json_mode=json_mode,
        system_instruction=system_instruction,
        temperature=temperature,
    )


def _generate_with_route(
    route: ModelRoute,
    prompt: str,
    *,
    json_mode: bool,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> ModelTextResponse:
    temperature = _temperature(temperature)
    key = _key(route)
    if route.provider in ("vertex_managed", "vertex_client", "gemini"):
        client = _genai_client(route, key)
        config = types.GenerateContentConfig(
            response_mime_type="application/json" if json_mode else None,
            system_instruction=system_instruction,
            temperature=temperature,
        )
        try:
            response = client.models.generate_content(
                model=route.model, contents=prompt, config=config
            )
        except Exception:
            # SDK errors can include a request URL or response details. The
            # admin sees a bounded error; logs must not stringify the key.
            error = (
                ProviderRequestError
                if route.provider != "vertex_managed"
                else RuntimeError
            )
            raise error(f"{route.provider} model request failed") from None
        from rubik_core.cost.usage import extract_usage

        try:
            text = response.text
            if not isinstance(text, str) or not text:
                raise ProviderRequestError("AI provider returned no text")
            usage = extract_usage(response)
        except ProviderRequestError:
            raise
        except Exception:
            raise ProviderRequestError(
                "AI provider returned an invalid response"
            ) from None
        if route.provider != "vertex_managed":
            _record(
                route,
                usage["prompt"],
                usage["completion"],
                thinking=usage["thinking"],
                cached=usage["cached"],
            )
        return ModelTextResponse(
            text,
            route.provider,
            route.model,
            route.billing_owner,
            usage["prompt"],
            usage["completion"],
        )
    return _remote_text(
        route,
        key,
        prompt,
        json_mode=json_mode,
        system_instruction=system_instruction,
        temperature=temperature,
    )


def generate_grounded_text_on_route(
    route: ModelRoute,
    prompt: str,
    *,
    system_instruction: str | None = None,
    temperature: float | None = None,
    timeout_s: float | None = None,
) -> ModelTextResponse:
    """Search-grounded text for a pinned native Google route.

    Gemini 2.5 does not support a JSON MIME constraint with Google Search.
    Callers that need JSON request it in the prompt and validate the answer.
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Prompt must be nonempty text")
    if system_instruction is not None and not isinstance(system_instruction, str):
        raise ValueError("System instruction must be text")
    live = resolve_model_route(route.feature, "grounding", default_model=route.model)
    if live != route:
        raise IncompatibleModelRoute("Workspace AI route changed during this request")
    return _generate_grounded_with_route(
        route,
        prompt,
        system_instruction=system_instruction,
        temperature=temperature,
        timeout_s=timeout_s,
    )


def _generate_grounded_with_route(
    route: ModelRoute,
    prompt: str,
    *,
    system_instruction: str | None = None,
    temperature: float | None = None,
    timeout_s: float | None = None,
) -> ModelTextResponse:
    if route.provider not in ("vertex_managed", "vertex_client", "gemini"):
        raise IncompatibleModelRoute(
            "Grounding adapter is unavailable for this provider"
        )
    client = _genai_client(route, _key(route))
    try:
        response = client.models.generate_content(
            model=route.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=_temperature(temperature),
                http_options=(
                    types.HttpOptions(timeout=int(timeout_s * 1000))
                    if timeout_s is not None
                    else None
                ),
            ),
        )
    except Exception:
        raise ProviderRequestError(
            f"{route.provider} grounding request failed"
        ) from None
    try:
        answer = getattr(response, "text", None)
    except Exception:
        raise ProviderRequestError(
            "AI provider returned an invalid grounded response"
        ) from None
    if not isinstance(answer, str) or not answer:
        raise ProviderRequestError("AI provider returned no grounded text")
    urls: list[str] = []
    try:
        for candidate in getattr(response, "candidates", None) or []:
            metadata = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(metadata, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None)
                if isinstance(uri, str) and uri.startswith(("https://", "http://")):
                    urls.append(uri)
    except Exception:
        # Grounding metadata is optional and must not change model success.
        urls = []
    from rubik_core.cost.usage import extract_usage

    try:
        usage = extract_usage(response)
    except Exception:
        raise ProviderRequestError(
            "AI provider returned invalid usage metadata"
        ) from None
    if route.provider != "vertex_managed":
        _record(
            route,
            usage["prompt"],
            usage["completion"],
            thinking=usage["thinking"],
            cached=usage["cached"],
        )
    return ModelTextResponse(
        answer,
        route.provider,
        route.model,
        route.billing_owner,
        usage["prompt"],
        usage["completion"],
        tuple(dict.fromkeys(urls)),
    )


async def generate_native_content_async_on_route(
    route: ModelRoute,
    contents: Any,
    *,
    capability: str,
    config: types.GenerateContentConfig | dict | None = None,
) -> Any:
    """Run a pinned image/audio request through the native Google SDK.

    The callers retain their existing Gemini content and config types. Non-Google
    providers remain unavailable until each media shape has an audited adapter.
    """
    if capability not in (
        "vision",
        "vision_json",
        "audio",
        "audio_json",
        "audio_tools",
        "native_tools",
    ):
        raise ValueError("Unsupported native media capability")

    def prepare():
        live = resolve_model_route(route.feature, capability, default_model=route.model)
        if live != route:
            raise IncompatibleModelRoute(
                "Workspace AI route changed during this request"
            )
        if route.provider not in ("vertex_managed", "vertex_client", "gemini"):
            raise IncompatibleModelRoute(
                "Media adapter is unavailable for this provider"
            )
        return _genai_client(route, _key(route))

    client = await asyncio.to_thread(prepare)
    try:
        response = await client.aio.models.generate_content(
            model=route.model, contents=contents, config=config
        )
    except Exception:
        raise ProviderRequestError(f"{route.provider} media request failed") from None
    if route.provider != "vertex_managed":
        from rubik_core.cost.usage import extract_usage

        try:
            usage = extract_usage(response)
        except Exception:
            raise ProviderRequestError(
                "AI provider returned invalid usage metadata"
            ) from None
        _record(
            route,
            usage["prompt"],
            usage["completion"],
            thinking=usage["thinking"],
            cached=usage["cached"],
        )
    return response


def _test_native_media(route: ModelRoute, *, capability: str) -> None:
    if route.provider not in ("vertex_managed", "vertex_client", "gemini"):
        raise IncompatibleModelRoute("Media adapter is unavailable for this provider")
    if capability in ("vision", "vision_json"):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/+1cAAAAASUVORK5CYII="
        )
        contents = [
            "Describe this image in one word.",
            types.Part.from_bytes(data=image, mime_type="image/png"),
        ]
    elif capability in ("audio", "audio_json", "audio_tools"):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(bytes(3_200))
        contents = [
            "Is there speech in this audio? Reply briefly.",
            types.Part.from_bytes(data=buffer.getvalue(), mime_type="audio/wav"),
        ]
    else:
        raise ValueError("Unsupported native media capability")
    if capability == "audio_tools":
        _test_native_tools(route, stream=False, media_part=contents[1])
        return
    json_mode = capability in ("vision_json", "audio_json")
    config = (
        types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=types.Schema(
                type=types.Type.OBJECT,
                properties={"summary": types.Schema(type=types.Type.STRING)},
                required=["summary"],
            ),
        )
        if json_mode
        else None
    )
    client = _genai_client(route, _key(route))
    responses = []
    try:
        response = client.models.generate_content(
            model=route.model, contents=contents, config=config
        )
        responses.append(response)
        if capability == "vision_json":
            responses.append(
                client.models.generate_content(
                    model=route.model,
                    contents=[
                        "Return a JSON object with a short summary of this PDF.",
                        types.Part.from_bytes(
                            data=_probe_pdf_bytes(), mime_type="application/pdf"
                        ),
                    ],
                    config=config,
                )
            )
    except Exception:
        raise ProviderRequestError(f"{route.provider} capability test failed") from None
    for response in responses:
        try:
            answer = response.text
        except Exception:
            raise ProviderRequestError(
                "AI provider did not complete the media test"
            ) from None
        if not isinstance(answer, str) or not answer:
            raise ProviderRequestError("AI provider did not complete the media test")
        if json_mode:
            try:
                parsed = json.loads(answer)
            except (TypeError, ValueError):
                raise ProviderRequestError(
                    "AI provider did not return media JSON"
                ) from None
            if not isinstance(parsed, dict) or not isinstance(
                parsed.get("summary"), str
            ):
                raise ProviderRequestError("AI provider did not return media JSON")
    if route.provider != "vertex_managed":
        from rubik_core.cost.usage import extract_usage

        for response in responses:
            usage = extract_usage(response)
            _record(
                route,
                usage["prompt"],
                usage["completion"],
                thinking=usage["thinking"],
                cached=usage["cached"],
            )


def _probe_pdf_bytes() -> bytes:
    """One-page in-memory PDF used only for capability activation."""
    content = b"BT /F1 12 Tf 10 50 Td (Cubie) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def test_connection(provider: str, model: str, *, capability: str = "text") -> None:
    """Probe the workload's actual capability without exposing its key."""
    if capability == "embedding":
        from rubik_core.ai_providers.embedding_index import embed_connection_probe

        embed_connection_probe(provider, model)
        return
    if capability in ("tools", "tools_stream", "native_tools"):
        route = provider_connection_route(provider, model, capability=capability)
        if capability == "native_tools" or provider in (
            "vertex_managed",
            "vertex_client",
            "gemini",
        ):
            _test_native_tools(route, stream=capability == "tools_stream")
        else:
            from rubik_core.ai_providers.adk_remote import test_remote_adk_connection

            asyncio.run(
                test_remote_adk_connection(route, stream=capability == "tools_stream")
            )
        return
    if capability == "grounding":
        route = provider_connection_route(provider, model, capability=capability)
        _generate_grounded_with_route(
            route,
            "Use Google Search and answer: What is the capital of Costa Rica?",
        )
        return
    if capability in ("vision", "vision_json", "audio", "audio_json", "audio_tools"):
        route = provider_connection_route(provider, model, capability=capability)
        _test_native_media(route, capability=capability)
        return
    if capability not in ("text", "json"):
        raise IncompatibleModelRoute("Connection test cannot verify this capability")
    route = provider_connection_route(provider, model, capability=capability)
    json_mode = capability == "json"
    prompt = 'Return the JSON object {"ok": true}.' if json_mode else "Reply with OK."
    response = _generate_with_route(route, prompt, json_mode=json_mode)
    if response.provider != provider:
        raise IncompatibleModelRoute("Connection test route does not match provider")
    if json_mode:
        try:
            parsed = json.loads(response.text)
        except ValueError:
            raise ProviderRequestError(
                "AI provider did not return a JSON object"
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderRequestError("AI provider did not return a JSON object")


def _test_native_tools(
    route: ModelRoute, *, stream: bool, media_part: types.Part | None = None
) -> None:
    """Exercise a function call and its response on the selected SDK client.

    Chat asks for both tool calls and streaming, so its activation probe uses
    the streaming method for both legs. A plain-text response to the first
    request is not evidence that the provider can call a tool.
    """
    if route.provider not in ("vertex_managed", "vertex_client", "gemini"):
        raise IncompatibleModelRoute("Tool adapter is unavailable for this provider")
    client = _genai_client(route, _key(route))
    instruction = "Call cubie_probe with an empty object, then say OK after its result."
    tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="cubie_probe",
                description="Verify function calling support.",
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ]
    )
    config = types.GenerateContentConfig(
        tools=[tool],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=["cubie_probe"],
            )
        ),
    )

    def call(contents, call_config):
        try:
            if stream:
                return list(
                    client.models.generate_content_stream(
                        model=route.model, contents=contents, config=call_config
                    )
                )
            return [
                client.models.generate_content(
                    model=route.model, contents=contents, config=call_config
                )
            ]
        except Exception:
            raise ProviderRequestError(
                f"{route.provider} capability test failed"
            ) from None

    prompt_parts = [types.Part.from_text(text=instruction)]
    if media_part is not None:
        prompt_parts.append(media_part)
    first_contents = (
        types.Content(role="user", parts=prompt_parts)
        if media_part is not None
        else instruction
    )
    first = call(first_contents, config)
    function_call = next(
        (
            item
            for response in first
            for item in (getattr(response, "function_calls", None) or [])
            if getattr(item, "name", None) == "cubie_probe"
        ),
        None,
    )
    if function_call is None:
        raise ProviderRequestError("AI provider did not call the test function")
    args = getattr(function_call, "args", None)
    if args is not None and not isinstance(args, dict):
        raise ProviderRequestError("AI provider returned an invalid function call")
    conversation = [
        types.Content(role="user", parts=prompt_parts),
        types.Content(
            role="model",
            parts=[types.Part.from_function_call(name="cubie_probe", args=args or {})],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="cubie_probe", response={"ok": True}
                )
            ],
        ),
    ]
    second = call(
        conversation,
        types.GenerateContentConfig(
            tools=[tool],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.NONE
                )
            ),
        ),
    )
    if not any(
        isinstance(getattr(response, "text", None), str) and response.text.strip()
        for response in second
    ):
        raise ProviderRequestError("AI provider did not complete the test function")
    if route.provider != "vertex_managed":
        from rubik_core.cost.usage import extract_usage

        for leg in (first, second):
            usage = extract_usage(leg[-1]) if leg else {}
            _record(
                route,
                usage.get("prompt", 0),
                usage.get("completion", 0),
                thinking=usage.get("thinking", 0),
                cached=usage.get("cached", 0),
            )


def parse_json_response(response: ModelTextResponse) -> Any:
    try:
        return json.loads(response.text)
    except ValueError as exc:
        raise RuntimeError("AI provider did not return valid JSON") from exc
