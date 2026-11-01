"""The Gemini Developer API as a transcription provider: one generate_content call with the audio.

A whole recording is uploaded through the Files API and deleted after the call; a chunk of a long recording, about 2 MB,
goes inline in the request.
"""

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
# Answers worth sending the same request again for: rate limits, timeouts and overloaded servers
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class ProviderError(Exception):
    def __init__(self, code: str, message: str, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        # A rate limit, a server error or a network failure, which the same request may not hit again
        self.transient = transient


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    # Of prompt_tokens, those of the audio; some models price audio above text
    prompt_audio_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
            self.prompt_audio_tokens + other.prompt_audio_tokens,
        )


@dataclass
class TranscriptionRequest:
    audio: Path
    mime_type: str
    system_instruction: str
    prompt: str
    max_output_tokens: int
    display_name: str
    # Send the audio in the request instead of uploading it to the Files API first
    inline: bool = False


@dataclass
class ProviderResponse:
    text: str | None
    # STOP, MAX_TOKENS, SAFETY, ...; None when the prompt was blocked and there is no candidate
    finish_reason: str | None
    block_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None
    response_id: str | None = None
    # The response's usage_metadata as Gemini reported it, see compact_usage_metadata
    usage_metadata: dict = field(default_factory=dict)

    def report(self) -> dict:
        """What Gemini reported about this request; Gemini reports no amount of money, only the usage"""
        report = {
            'model_version': self.model_version,
            'response_id': self.response_id,
            'finish_reason': self.finish_reason,
            'block_reason': self.block_reason,
        }
        return {key: value for key, value in report.items() if value is not None} | {
            'usage_metadata': self.usage_metadata
        }


def compact_usage_metadata(meta: types.GenerateContentResponseUsageMetadata | None) -> dict:
    """The fields Gemini set, a per-modality list such as prompt_tokens_details as {modality: tokens}"""
    if meta is None:
        return {}
    result = {}
    for key, value in meta.model_dump(mode='json', exclude_none=True).items():
        if isinstance(value, list):
            value = {
                str(item.get('modality')): item.get('token_count') or 0 for item in value if isinstance(item, dict)
            }
        result[key] = value
    return result


def summed_usage_metadata(entries: list[dict]) -> dict:
    """The usage_metadata of several requests added up; a text field such as traffic_type is kept when all requests
    agree, and a list of the values otherwise"""
    result: dict = {}
    for entry in entries:
        for key, value in entry.items():
            current = result.get(key)
            if isinstance(value, dict):
                current = dict(current or {})
                for modality, tokens in value.items():
                    current[modality] = current.get(modality, 0) + tokens
                result[key] = current
            elif isinstance(value, int | float) and not isinstance(value, bool):
                result[key] = (current or 0) + value
            else:
                values = current if isinstance(current, list) else ([] if current is None else [current])
                if value not in values:
                    values.append(value)
                result[key] = values[0] if len(values) == 1 else values
    return result


def summary_report(reports: list[dict]) -> dict:
    """The provider report of a transcription made of these requests' reports. The response ID of each request of a
    chunked transcription is in its chunk's attempts, only that of a single request is repeated here."""
    finish_reasons: dict[str, int] = {}
    for report in reports:
        reason = report.get('finish_reason') or report.get('block_reason') or 'none'
        finish_reasons[reason] = finish_reasons.get(reason, 0) + 1
    summary = {
        'requests': len(reports),
        'model_versions': sorted({r['model_version'] for r in reports if r.get('model_version')}),
        'finish_reasons': finish_reasons,
        'usage_metadata': summed_usage_metadata([r.get('usage_metadata') or {} for r in reports]),
    }
    if len(reports) == 1 and reports[0].get('response_id'):
        summary['response_id'] = reports[0]['response_id']
    return summary


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
            if request.inline:
                audio_part = types.Part.from_bytes(data=request.audio.read_bytes(), mime_type=request.mime_type)
            else:
                uploaded = await client.files.upload(
                    file=request.audio,
                    config=types.UploadFileConfig(mime_type=request.mime_type, display_name=request.display_name),
                )
                uploaded = await self.wait_until_active(uploaded)
                audio_part = types.Part.from_uri(file_uri=uploaded.uri, mime_type=request.mime_type)
            response = await client.models.generate_content(
                model=self.model,
                contents=[audio_part, request.prompt],
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
            transient = e.code in TRANSIENT_STATUS_CODES
            raise ProviderError(
                'rate_limited' if e.code == 429 else 'provider_error', error_message(e), transient
            ) from e
        except httpx.HTTPError as e:
            message = f'Could not reach Gemini: {type(e).__name__}: {e}'[:500]
            raise ProviderError('provider_error', message, transient=True) from e
        except OSError as e:
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
            prompt_audio_tokens=sum(
                detail.token_count or 0
                for detail in (meta and meta.prompt_tokens_details) or []
                if detail.modality == types.MediaModality.AUDIO
            ),
        )
        block_reason = enum_name(response.prompt_feedback.block_reason) if response.prompt_feedback else None
        ids = {
            'model_version': response.model_version,
            'response_id': response.response_id,
            'usage_metadata': compact_usage_metadata(meta),
        }
        if not response.candidates:
            return ProviderResponse(None, None, block_reason, usage, **ids)
        candidate = response.candidates[0]
        parts = (candidate.content and candidate.content.parts) or []
        text = ''.join(part.text for part in parts if part.text and not part.thought) or None
        return ProviderResponse(text, enum_name(candidate.finish_reason), block_reason, usage, **ids)
