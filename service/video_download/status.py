"""The status of a job as the API returns it: the job's metadata with its files, transcriptions and their cost"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from video_download import config, transcription
from video_download.storage import job_artifacts, media_files


class Artifact(BaseModel):
    name: str
    kind: str  # media, subtitle, info, transcript
    size: int | None = None
    media: str | None = None  # transcript: the media file it was made from
    lang: str | None = None  # subtitle track key, e.g. en, en-US, cs-orig
    auto: bool | None = None  # subtitle is automatic captions


class DeletedFile(BaseModel):
    name: str
    kind: str
    size: int | None = None
    deleted_at: str
    reason: str  # user, gc


class TranscriptionUsage(BaseModel):
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    prompt_audio_tokens: int | None = None  # None for transcriptions stored before it was recorded
    audio_seconds: float | None = None  # a provider that bills the length of the audio, not tokens


class TranscriptionPrices(BaseModel):
    model: str | None = None
    # USD per million tokens, or per hour of audio
    input_text: float | None = None
    input_audio: float | None = None
    output: float | None = None
    per_hour: float | None = None
    as_of: str | None = None
    source: str | None = None


class TranscriptionCost(BaseModel):
    usd: float
    # False when the provider reported the amount (source provider), True for an estimate from usage and list prices
    estimated: bool = True
    source: str | None = None
    # The estimate next to an amount the provider reported
    estimate_usd: float | None = None
    # The list prices of the estimate, None when the provider reported the amount and the model has no known price
    prices: TranscriptionPrices | None = None


class ReportedCost(BaseModel):
    amount: float
    currency: str  # lower case, e.g. usd


class ProviderReport(BaseModel):
    """What the provider itself reported about the transcription's requests; None for transcriptions stored before it
    was recorded. ElevenLabs fills the first group of fields, Gemini the second."""

    transcription_id: str | None = None
    audio_seconds: float | None = None
    credits: int | float | None = None
    cost: ReportedCost | None = None
    region: str | None = None
    trace_id: str | None = None

    requests: int | None = None
    model_versions: list[str] | None = None
    response_id: str | None = None  # of a single request; a chunked transcription has them in its chunks' attempts
    finish_reasons: dict[str, int] | None = None
    usage_metadata: dict[str, Any] | None = None  # summed over the requests, per-modality lists as {modality: tokens}


class TranscriptionWarning(BaseModel):
    code: str
    message: str


class Transcription(BaseModel):
    status: str  # pending, running, completed, failed
    provider: str | None = None  # gemini, elevenlabs; None for transcriptions stored before it was recorded
    model: str | None = None
    requested_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    error_code: str | None = None
    warnings: list[TranscriptionWarning] = []
    usage: TranscriptionUsage | None = None
    # Reported by the provider, or estimated from usage and list prices; None for a model without a known price
    cost: TranscriptionCost | None = None
    provider_report: ProviderReport | None = None
    files: list[str] = []


class ExpectedSubtitle(BaseModel):
    lang: str
    auto: bool


class Progress(BaseModel):
    """yt-dlp's progress of a running download; the sizes, percent, speed and ETA are of the part being downloaded"""

    phase: str = Field(description='downloading, merging, postprocessing or subtitles')
    part: str | None = Field(None, description='video or audio, when they download separately before a merge')
    part_index: int | None = Field(None, description='1-based, of part_count')
    part_count: int | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    total_estimated: bool = Field(False, description="total_bytes is yt-dlp's estimate")
    percent: float | None = None
    speed: float | None = Field(None, description='bytes per second')
    eta: int | None = Field(None, description='seconds')
    fragment_index: int | None = None
    fragment_count: int | None = None
    filename: str | None = None
    updated_at: str | None = None


class DownloadStatus(BaseModel):
    download_id: str
    status: str  # pending, downloading, completed, failed
    url: str
    download_url: str | None = None
    extractor: str | None = None
    files: list[str] = []
    artifacts: list[Artifact] = []
    deleted_files: list[DeletedFile] = []
    error: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    used_youtube_cookies: bool | None = None
    subtitles_error: str | None = None
    transcriptions: dict[str, Transcription] = {}
    # Whether the server can transcribe at all (a provider and its key are set); the page disables its Transcribe
    # buttons if not
    transcription_available: bool = False
    created_at: str
    updated_at: str
    completed_at: str | None = None
    # Video metadata fields (optional)
    title: str | None = None
    duration: int | None = None
    uploader: str | None = None
    upload_date: str | None = None
    description: str | None = None
    ext: str | None = None
    format: str | None = None
    resolution: str | None = None
    thumbnail: str | None = None
    webpage_url: str | None = None
    id: str | None = None
    channel: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    # Known right after the extraction, before the download ends
    filesize_approx: int | None = None
    chapters_count: int | None = None
    expected_subtitles: list[ExpectedSubtitle] | None = None
    progress: Progress | None = None


def status_response(download_dir: Path, metadata: dict) -> DownloadStatus:
    return DownloadStatus(
        **(
            metadata
            | {
                'files': media_files(metadata),
                'artifacts': job_artifacts(download_dir, metadata),
                'transcriptions': transcription.transcriptions_with_cost(metadata),
                'transcription_available': config.transcription_settings().available,
            }
        )
    )
