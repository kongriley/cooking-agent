"""OpenAI's Responses API, for when there's an OpenAI key but no Anthropic one: Basil's supervisor, the shopping
agent's computer use, and checking photos all have an OpenAI path. Plain HTTP, like the image code, so there's no
extra SDK to install.
"""

import asyncio
import logging
import os
import ssl

import aiohttp

RESPONSES_URL = "https://api.openai.com/v1/responses"
# One model for all of it; it supports the computer tool and image input. Override with BASIL_OPENAI_MODEL.
MODEL = os.environ.get("BASIL_OPENAI_MODEL", "gpt-6.1-sol")
TIMEOUT = aiohttp.ClientTimeout(total=180)


def available() -> bool:
    return "OPENAI_API_KEY" in os.environ


# A dropped or garbled connection (a VPN or Wi-Fi blip shows up as "SSL: BAD_RECORD_MAC") or a busy API is worth a
# couple more tries; a shopping run is minutes of work to lose to one bad packet.
ATTEMPTS = 4
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


async def respond(http: aiohttp.ClientSession, body: dict) -> dict:
    """One Responses API call, retried through network blips and busy moments; raises with OpenAI's own message."""
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    for attempt in range(ATTEMPTS):
        try:
            async with http.post(RESPONSES_URL, json={"model": MODEL, **body}, headers=headers) as response:
                data = await response.json(content_type=None)
                if response.status in RETRY_STATUSES and attempt < ATTEMPTS - 1:
                    raise _Retry(f"OpenAI said {response.status}")
                if response.status >= 400:
                    raise aiohttp.ClientResponseError(
                        response.request_info, (), status=response.status, message=str(data.get("error", data))[:300]
                    )
                return data
        except (_Retry, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, ssl.SSLError, TimeoutError) as e:
            if attempt == ATTEMPTS - 1:
                raise
            logging.warning(f"OpenAI request failed ({e}); trying again")
            await asyncio.sleep(2**attempt)
    raise AssertionError("unreachable")


class _Retry(Exception):
    pass


def text(response: dict) -> str:
    """The model's reply text, from the message items of a response."""
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ).strip()


async def ask(
    prompt: str, instructions: str | None = None, images: list[tuple[str, str]] = (), effort: str = "low"
) -> str:
    """A one-off question, optionally about images ((media_type, base64 data) pairs); returns the reply text."""
    content = [
        {"type": "input_image", "image_url": f"data:{kind};base64,{data}", "detail": "low"} for kind, data in images
    ]
    body = {
        "input": [{"role": "user", "content": [*content, {"type": "input_text", "text": prompt}]}],
        "reasoning": {"effort": effort},
    }
    if instructions:
        body["instructions"] = instructions
    async with aiohttp.ClientSession(timeout=TIMEOUT) as http:
        return text(await respond(http, body))
