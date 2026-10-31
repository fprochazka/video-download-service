"""The Gemini Developer API as a transcription provider: upload the audio, one generate_content call, delete the upload"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

# One request of an hour of audio takes minutes; the SDK default has no timeout at all
HTTP_TIMEOUT_MS = 15 * 60 * 1000
# Audio uploads are usually ACTIVE at once, the Files API processes them in the background otherwise
FILE_ACTIVE_TIMEOUT_SECONDS = 300
FILE_POLL_SECONDS = 2


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
        )


@dataclass
class TranscriptionRequest:
    audio: Path
    mime_type: str
    system_instruction: str
    prompt: str
    max_output_tokens: int
    display_name: str


@dataclass
class ProviderResponse:
    text: str | None
    # STOP, MAX_TOKENS, SAFETY, ...; None when the prompt was blocked and there is no candidate
    finish_reason: str | None
    block_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_MS))


def enum_name(value) -> str | None:
    if value is None:
        return None
    return getattr(value, 'name', None) or str(value)


def error_message(e: errors.APIError) -> str:
    message = f'Gemini API error {e.code}'
    if e.status:
        message += f' {e.status}'
    if e.message:
        message += f': {e.message}'
    # A 400 often says only "invalid argument", the details name the field
    details = ((e.details or {}).get('error') or {}).get('details') if isinstance(e.details, dict) else None
    if details:
        message += f' {json.dumps(details, ensure_ascii=False)}'
    return message[:1000]


class GeminiProvider:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = make_client(api_key)

    async def transcribe(self, request: TranscriptionRequest) -> ProviderResponse:
        """Raises ProviderError with code rate_limited or provider_error"""
        client = self.client.aio
        uploaded = None
        try:
            uploaded = await client.files.upload(
                file=request.audio,
                config=types.UploadFileConfig(mime_type=request.mime_type, display_name=request.display_name),
            )
            uploaded = await self.wait_until_active(uploaded)
            response = await client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_uri(file_uri=uploaded.uri, mime_type=request.mime_type), request.prompt],
                config=types.GenerateContentConfig(
                    system_instruction=request.system_instruction,
                    max_output_tokens=request.max_output_tokens,
                    # No thinking_config: Flash-Lite thinks at its minimum by default, usage records the thinking
                    # tokens. Temperature stays at the default too, temperature 0 truncates output on Gemini 3.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            return self.provider_response(response)
        except errors.APIError as e:
            raise ProviderError('rate_limited' if e.code == 429 else 'provider_error', error_message(e)) from e
        except (httpx.HTTPError, OSError) as e:
            raise ProviderError('provider_error', f'Could not reach Gemini: {type(e).__name__}: {e}'[:500]) from e
        finally:
            if uploaded is not None and uploaded.name:
                await self.delete_upload(uploaded.name)

    async def wait_until_active(self, uploaded: types.File) -> types.File:
        deadline = time.monotonic() + FILE_ACTIVE_TIMEOUT_SECONDS
        while uploaded.state == types.FileState.PROCESSING:
            if time.monotonic() > deadline:
                raise ProviderError('provider_error', 'The uploaded audio did not become ready in time')
            await asyncio.sleep(FILE_POLL_SECONDS)
            uploaded = await self.client.aio.files.get(name=uploaded.name)
        if uploaded.state == types.FileState.FAILED:
            raise ProviderError('provider_error', 'Gemini could not process the uploaded audio')
        return uploaded

    async def delete_upload(self, name: str):
        """The Files API would delete it after 48 hours anyway, a failure here does not fail the transcription"""
        try:
            await self.client.aio.files.delete(name=name)
        except Exception:
            logger.warning('Could not delete the uploaded audio %s from Gemini', name, exc_info=True)

    @staticmethod
    def provider_response(response: types.GenerateContentResponse) -> ProviderResponse:
        meta = response.usage_metadata
        usage = Usage(
            prompt_tokens=(meta and meta.prompt_token_count) or 0,
            output_tokens=(meta and meta.candidates_token_count) or 0,
            thinking_tokens=(meta and meta.thoughts_token_count) or 0,
        )
        block_reason = enum_name(response.prompt_feedback.block_reason) if response.prompt_feedback else None
        if not response.candidates:
            return ProviderResponse(None, None, block_reason, usage)
        candidate = response.candidates[0]
        parts = (candidate.content and candidate.content.parts) or []
        text = ''.join(part.text for part in parts if part.text and not part.thought) or None
        return ProviderResponse(text, enum_name(candidate.finish_reason), block_reason, usage)
