"""
LLM client wrapper. Uses the OpenAI-compatible chat completions API,
so it works unmodified with Groq, OpenAI, Together, or a local Ollama
server just by changing LLM_BASE_URL / LLM_MODEL in .env — no
provider-specific billing integration needed.
"""

import os
import json
import re
import threading
import time
from dotenv import load_dotenv
from openai import OpenAI
from openai import (
    APIError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

_client = None

# Client-side pacing — Groq free tier: 30 RPM / 6 000 TPM.
# 30 RPM = 1 request per 2 seconds minimum. We use 0.5s so the 3-4 call
# pipeline completes in ~2s wait instead of ~8s, while staying well under
# the 30 RPM cap (4 calls per question = ~4 RPM actual usage).
_MIN_REQUEST_INTERVAL = float(os.getenv("LLM_MIN_REQUEST_INTERVAL", "0.5"))
_pacing_lock = threading.Lock()
_last_call_at = 0.0


def _pace_request() -> None:
    global _last_call_at
    with _pacing_lock:
        wait = (_last_call_at + _MIN_REQUEST_INTERVAL) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _retry_after_seconds(exc: Exception, default: float) -> float:
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            return max(float(header), 0.5)
        except ValueError:
            pass
    match = re.search(r"try again in ([\d.]+)s", str(exc), flags=re.IGNORECASE)
    if match:
        return max(float(match.group(1)), 0.5)
    return default


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not api_key or api_key == "your_api_key_here":
            raise RuntimeError("LLM_API_KEY is missing or still uses the placeholder value.")
        _client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=api_key,
            timeout=float(os.getenv("LLM_TIMEOUT", "45")),
            max_retries=1,
        )
    return _client


def _model_name() -> str:
    return os.getenv("LLM_MODEL", "llama-3.1-8b-instant")


_MAX_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "4"))


def _call_once(client, messages, temperature, kwargs):
    _pace_request()
    return client.chat.completions.create(
        model=_model_name(),
        messages=messages,
        temperature=temperature,
        **kwargs,
    )


def chat(
    messages: list[dict],
    temperature: float = 0.2,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """Send a chat completion request, handling rate limits and retries."""
    client = get_client()
    if max_tokens is None:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1200"))
    kwargs: dict = {"max_tokens": max_tokens}

    response = None
    rate_limit_attempts = 0
    while response is None:
        try:
            response = _call_once(client, messages, temperature, kwargs)
        except BadRequestError as e:
            if json_mode:
                kwargs.pop("response_format", None)
                try:
                    response = _call_once(client, messages, temperature, kwargs)
                except BadRequestError as retry_err:
                    raise RuntimeError(
                        f"LLM provider rejected the request: {retry_err}"
                    ) from retry_err
            else:
                raise RuntimeError(f"LLM provider rejected the request: {e}") from e
        except AuthenticationError as e:
            raise RuntimeError(
                "LLM_API_KEY was rejected by the provider. Check backend/.env."
            ) from e
        except RateLimitError as e:
            rate_limit_attempts += 1
            if rate_limit_attempts > _MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(
                    "LLM provider is still rate-limiting after "
                    f"{_MAX_RATE_LIMIT_RETRIES} retries. Wait and try again, "
                    "or lower TOP_K / MAX_CORRECTION_ROUNDS in .env."
                ) from e
            wait = _retry_after_seconds(e, default=2.0 * rate_limit_attempts)
            time.sleep(wait)
        except APIError as e:
            raise RuntimeError(f"LLM provider error: {e}") from e

    content = response.choices[0].message.content
    return content if content is not None else ""


def safe_json_parse(text: str, fallback):
    """Parse LLM JSON output; tolerates markdown fences and minor formatting."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = re.sub(r"^json\s*", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return fallback


def vision_chat(image_path: str, prompt: str, max_tokens: int = 1200) -> str:
    """
    Send an image to a multimodal LLM (base64 data URL).
    Works with Groq llama-4-scout / llama-4-maverick and OpenAI GPT-4o.
    Set LLM_VISION_MODEL in .env to override the model.
    """
    import base64
    import mimetypes

    mime, _ = mimetypes.guess_type(image_path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    data_url = f"data:{mime};base64,{b64}"
    vision_model = os.getenv(
        "LLM_VISION_MODEL",
        os.getenv("LLM_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
    )

    client = get_client()
    _pace_request()
    try:
        response = client.chat.completions.create(
            model=vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        content = response.choices[0].message.content
        return content if content else ""
    except Exception as exc:
        raise RuntimeError(f"Vision API call failed: {exc}") from exc
