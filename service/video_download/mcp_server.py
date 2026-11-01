"""The MCP server at /mcp: a tool to download a video, tools to follow and transcribe its job, the job's text as resources.

Every tool works on the same jobs as the web page, through jobs.submit_download and transcription.submit_transcription,
and reads their state from metadata.json. download_video answers at once with the job, and the agent follows it with
get_job, which can wait for the next change; a client of MCP tasks gets a task instead, see mcp_tasks.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools import ToolResult
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from video_download import config, jobs, pricing, transcription
from video_download.status import ExpectedSubtitle, Progress, ProviderReport, status_response
from video_download.storage import LOG_FILENAME, TRANSCRIPT, load_metadata
from video_download.urls import InvalidDownloadUrl
from video_download.worker import JobWorker

logger = logging.getLogger(__name__)

RESOURCE_SCHEME = 'video-download'

# get_job checks metadata.json this often while it waits, and waits at most this long
WAIT_POLL_SECONDS = 0.5
MAX_WAIT_SECONDS = 60

TERMINAL_STATES = ('completed', 'failed')

VideoUrl = Annotated[
    str,
    Field(description='An http(s) link to the page of ONE video, or of one post'),
]
ForceDownload = Annotated[
    bool,
    Field(description='Download again even when the link was already downloaded'),
]
DownloadId = Annotated[
    str,
    Field(description='The job id (a UUID) that download_video returned', json_schema_extra={'format': 'uuid'}),
]
WaitSeconds = Annotated[
    int,
    Field(
        ge=0,
        le=MAX_WAIT_SECONDS,
        description='Wait up to this many seconds for the job to change or end, 0 answers at once',
    ),
]
MediaFilename = Annotated[
    str,
    Field(description="The name of one of the job's video or audio files, a file of kind media in get_job's files"),
]


def instructions(transcription: bool) -> str:
    """The server instructions; without transcription configured nothing mentions it"""
    lines = [
        'Downloads videos from the sites yt-dlp supports' + (' and transcribes them.' if transcription else '.'),
        'Call download_video with a link to one video or post.',
        *(
            [
                "Prefer the job's subtitle files, they come free with the download. Call transcribe_file for a "
                'transcript, which costs money, when the user asks for one, or when the subtitles are missing or look '
                'broken (truncated, in the wrong language, garbled).'
            ]
            if transcription
            else []
        ),
        'When download_video answers with a job that has not finished yet, call get_job with its download_id and '
        'wait_seconds=60 until the state is completed or failed.',
        'The result lists the files with download URLs'
        + (
            "; a transcript is also readable as the resource named in the job's transcriptions."
            if transcription
            else '.'
        ),
    ]
    return ' '.join(lines)


def download_video_description(transcription: bool) -> str:
    files = 'video, subtitles, info.json, transcripts' if transcription else 'video, subtitles, info.json'
    resources = (
        'the metadata, the download log and the transcripts' if transcription else 'the metadata and the download log'
    )
    return '\n'.join(
        [
            'Download one video, or one post with several videos, from any site yt-dlp supports: video platforms, '
            'social networks and more.',
            'Pass a link to that one video or post. Do not pass links to playlists, channels, profiles or other '
            'collections, they are rejected even when they hold one video. A video link that also refers to a playlist '
            'downloads only the video.',
            'A download takes from seconds to several minutes.',
            'When the same video was already downloaded and its video file is still there, or its download still runs, '
            'the answer is that job with existing true and nothing is downloaded; force=true downloads it again.',
            'If the answer is a job that has not finished yet, follow it with get_job, using wait_seconds to wait.',
            *(
                [
                    "Subtitles in the video's language and in English come with the download when the site has them. "
                    'Prefer them; transcribe a video file with transcribe_file when the user asks for a transcript, or '
                    'when the subtitles are missing or look broken (truncated, in the wrong language, garbled).'
                ]
                if transcription
                else []
            ),
            f'The result lists every file ({files}) with a plain HTTP download URL, unauthenticated and reachable on '
            f'the private network, and video-download://jobs/{{id}}/... resources for {resources}.',
        ]
    )


def get_job_description(transcription: bool) -> str:
    states = (
        'pending, downloading, transcribing (the download completed, a transcription runs), completed or failed'
        if transcription
        else 'pending, downloading, completed or failed'
    )
    contents = 'the video details, the download progress while it runs, the files with kind, size and download URL, '
    if transcription:
        contents += 'each transcription with its state and cost, '
    return '\n'.join(
        [
            f'The state of a job: {states}.',
            f'The result lists {contents}and error and error_code when the job failed.',
            'With wait_seconds the call answers once the job changes or ends, or after that many seconds.',
        ]
    )


# Measured on the default model, see README; a price per token says little before the transcript is written
GEMINI_PRICE_TEXT = 'about $0.13 per hour of speech'


def transcription_price(settings: config.TranscriptionSettings) -> str | None:
    """What a transcription costs with the configured model, None when the service cannot tell"""
    if settings.provider == 'gemini':
        return GEMINI_PRICE_TEXT if settings.model == config.PROVIDERS['gemini'].default_model else None
    price = pricing.price_of(settings.model, datetime.now(UTC).date())
    if price is not None and price.per_hour is not None:
        return f'${price.per_hour:.2f} per hour of audio'
    return None


def transcribe_file_description(settings: config.TranscriptionSettings) -> str:
    price = transcription_price(settings)
    return '\n'.join(
        [
            f'Transcribe one video or audio file of a completed job with {settings.info.name}'
            + (f', {price}.' if price else ', which costs money.'),
            'Use it when the user asks for a transcript, or when the job has no subtitle files or they look broken: '
            'truncated, in the wrong language or garbled. Otherwise prefer the subtitles.',
            'A new transcript replaces an earlier one of the same file.',
            'The call answers at once, follow the transcription with get_job.',
        ]
    )


def resource_uri(download_id: str, *parts: str) -> str:
    return f'{RESOURCE_SCHEME}://jobs/{download_id}/' + '/'.join(quote(part, safe='') for part in parts)


def request_base_url() -> str | None:
    """PUBLIC_BASE_URL, or the scheme and host the HTTP request came to; None outside of an HTTP request"""
    if base_url := config.public_base_url():
        return base_url
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    # Behind the Tailscale proxy the request arrives as http, the forwarded headers carry what the client used
    proto = (request.headers.get('x-forwarded-proto') or request.url.scheme).split(',')[0].strip()
    host = (request.headers.get('x-forwarded-host') or request.headers.get('host') or '').split(',')[0].strip()
    return f'{proto}://{host}' if host else None


def job_dir(download_id: str) -> tuple[str, Path]:
    """The normalised id and directory of an existing job; ToolError for an id that is not a job"""
    try:
        normalised = str(uuid.UUID(download_id.strip()))
    except ValueError:
        raise ToolError(f'Unknown download_id {download_id!r}, it is not a job id.') from None
    download_dir = config.DOWNLOADS_DIR / normalised
    if load_metadata(download_dir) is None:
        raise ToolError(f'Unknown download_id {download_id!r}, there is no such job.')
    return normalised, download_dir


def job_state(metadata: dict) -> tuple[str, str]:
    """(state, message): the download's status, or transcribing while a transcription of the job is queued or running"""
    status = metadata.get('status')
    if status == 'pending':
        return 'pending', 'queued'
    if status == 'downloading':
        return 'downloading', downloading_message(metadata.get('progress'))
    if status == 'failed':
        return 'failed', f'failed ({metadata.get("error_code") or "unknown"}): {metadata.get("error") or ""}'.strip()
    states = [state or {} for state in (metadata.get('transcriptions') or {}).values()]
    active = [state for state in states if state.get('status') in transcription.ACTIVE_TRANSCRIPTION_STATUSES]
    if active:
        current = min(len(states) - len(active) + 1, len(states))
        return 'transcribing', f'transcribing {current}/{len(states)}'
    return 'completed', 'completed'


def downloading_message(progress: dict | None) -> str:
    """downloading, with the percent once yt-dlp reports it: downloading 42%, downloading 42% (audio 2/2), merging"""
    if not progress:
        return 'downloading'
    phase = progress.get('phase')
    if phase in ('merging', 'postprocessing'):
        return f'downloading, {phase}'
    message = 'downloading subtitles' if phase == 'subtitles' else 'downloading'
    if progress.get('percent') is not None:
        message += f' {progress["percent"]:.0f}%'
    if progress.get('part_index') and progress.get('part_count'):
        part = f'{progress.get("part") or "part"} {progress["part_index"]}/{progress["part_count"]}'
        message += f' ({part})'
    return message


def format_size(size: int | None) -> str:
    if size is None:
        return 'size unknown'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return ''


class JobFile(BaseModel):
    name: str
    kind: str = Field(description='media (video or audio), subtitle, info (info.json) or transcript')
    size: int | None = Field(description='bytes')
    url: str = Field(description='plain HTTP download URL, no authentication')
    media: str | None = Field(None, description='transcript: the media file it was made from')
    lang: str | None = Field(None, description='subtitle: the track language, e.g. en or cs-orig')
    auto_captions: bool | None = Field(None, description='subtitle: automatic captions')
    resource_uri: str | None = Field(None, description='transcript: the MCP resource with its text')


class JobTranscription(BaseModel):
    status: str = Field(description='pending, running, completed or failed')
    model: str | None
    error: str | None
    error_code: str | None
    warnings: list[str]
    cost_usd: float | None = Field(
        description='the amount the provider reported when cost_estimated is false, an estimate from the usage and '
        'list prices otherwise'
    )
    cost_estimated: bool | None
    provider_report: ProviderReport | None = Field(
        description='what the provider reported about its requests: ElevenLabs its cost, credits and transcription_id, '
        'Gemini its token usage, model versions and finish reasons'
    )
    files: list[str]
    transcript_resource_uri: str | None


class JobResources(BaseModel):
    metadata: str
    log: str


class Job(BaseModel):
    download_id: str
    state: str = Field(description='pending, downloading, transcribing, completed or failed')
    done: bool = Field(description='the state is completed or failed')
    message: str
    url: str
    job_url: str = Field(description='the job page in a browser')
    title: str | None
    duration: int | None = Field(description='seconds')
    uploader: str | None
    channel: str | None
    upload_date: str | None = Field(description='YYYYMMDD')
    thumbnail: str | None = Field(description='image URL on the site')
    webpage_url: str | None
    resolution: str | None
    filesize_approx: int | None = Field(description='bytes, estimated before the download')
    chapters_count: int | None
    expected_subtitles: list[ExpectedSubtitle] | None = Field(description='the subtitle tracks the download fetches')
    progress: Progress | None = Field(description='while downloading')
    error: str | None
    error_code: str | None
    transcription_available: bool
    files: list[JobFile]
    transcriptions: dict[str, JobTranscription] = Field(description='by media file name')
    resources: JobResources
    created_at: str
    updated_at: str
    existing: bool = Field(
        False, description='download_video found this job of the same video instead of starting a download'
    )


def without_titles(schema):
    """The schema without the titles pydantic derives from the field names, which every agent would read for nothing"""
    if isinstance(schema, list):
        return [without_titles(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == 'title' and isinstance(value, str):
            continue
        if key in ('properties', '$defs'):
            result[key] = {name: without_titles(item) for name, item in value.items()}
        else:
            result[key] = without_titles(value)
    return result


# The structured content of every tool result, advertised as each tool's outputSchema
JOB_SCHEMA = without_titles(Job.model_json_schema())


def job_summary(download_dir: Path, metadata: dict, base_url: str | None, existing: bool = False) -> dict:
    """The job as get_job returns it: state, video details, files with URLs, transcriptions with cost, resource URIs"""
    download_id = metadata['download_id']
    status = status_response(download_dir, metadata)
    state, message = job_state(metadata)
    base = base_url or ''
    files = [
        JobFile(
            name=entry.name,
            kind=entry.kind,
            size=entry.size,
            url=f'{base}/downloads/{download_id}/{quote(entry.name)}',
            media=entry.media,
            lang=entry.lang,
            auto_captions=bool(entry.auto) if entry.lang else None,
            resource_uri=resource_uri(download_id, 'transcripts', entry.name) if entry.kind == TRANSCRIPT else None,
        )
        for entry in status.artifacts
    ]
    transcriptions = {
        media_name: JobTranscription(
            status=item.status,
            model=item.model,
            error=item.error,
            error_code=item.error_code,
            warnings=[warning.message for warning in item.warnings],
            cost_usd=item.cost.usd if item.cost else None,
            cost_estimated=item.cost.estimated if item.cost else None,
            provider_report=item.provider_report,
            files=item.files,
            transcript_resource_uri=next(
                (resource_uri(download_id, 'transcripts', name) for name in item.files if name.endswith('.md')), None
            ),
        )
        for media_name, item in status.transcriptions.items()
    }
    job = Job(
        download_id=download_id,
        state=state,
        done=state in TERMINAL_STATES,
        message=message,
        url=status.url,
        job_url=f'{base}/jobs/{download_id}',
        title=status.title,
        duration=status.duration,
        uploader=status.uploader,
        channel=status.channel,
        upload_date=status.upload_date,
        thumbnail=status.thumbnail,
        webpage_url=status.webpage_url,
        resolution=status.resolution,
        filesize_approx=status.filesize_approx,
        chapters_count=status.chapters_count,
        expected_subtitles=status.expected_subtitles,
        progress=status.progress,
        error=status.error,
        error_code=status.error_code,
        transcription_available=status.transcription_available,
        files=files,
        transcriptions=transcriptions,
        resources=JobResources(metadata=resource_uri(download_id, 'metadata'), log=resource_uri(download_id, 'log')),
        created_at=status.created_at,
        updated_at=status.updated_at,
        existing=existing,
    )
    return job.model_dump(mode='json')


def summary_text(summary: dict) -> str:
    """The short human-readable part of a tool result"""
    download_id = summary['download_id']
    details = [f'"{summary["title"]}"' if summary['title'] else None]
    if summary['duration']:
        details.append(f'{summary["duration"]} s')
    if summary['uploader']:
        details.append(f'by {summary["uploader"]}')
    lines = [
        f'Job {download_id}: {summary["message"]}. {" ".join(d for d in details if d)}'.rstrip(),
        f'Page: {summary["job_url"]}',
    ]
    if summary['existing']:
        lines.append(
            f'The link was already downloaded in this job, created {summary["created_at"]}; nothing new was '
            'downloaded. Call download_video with force=true to download it again.'
        )
    if summary['files']:
        lines.append('Files:')
        for file in summary['files']:
            line = f'- {file["kind"]} {file["name"]} ({format_size(file["size"])}): {file["url"]}'
            if file.get('resource_uri'):
                line += f' (resource {file["resource_uri"]})'
            lines.append(line)
    for media_name, item in summary['transcriptions'].items():
        line = f'Transcription of {media_name}: {item["status"]}'
        if item['error']:
            line += f' ({item["error_code"]}): {item["error"]}'
        if item['cost_usd'] is not None and item['cost_estimated'] is False:
            line += f', cost ${item["cost_usd"]:.6f} as reported by the provider'
        elif item['cost_usd'] is not None:
            line += f', estimated cost ${item["cost_usd"]:.4f}'
        lines.append(line)
        lines += [f'  warning: {warning}' for warning in item['warnings']]
    if not summary['done']:
        lines.append(
            f'The job is still running. Call get_job with download_id="{download_id}" and wait_seconds=60 '
            'to wait for its next change.'
        )
    return '\n'.join(lines)


def resource_links(summary: dict) -> list[mcp_types.ResourceLink]:
    """The job's files as links: their download URL when it is absolute, and the transcripts' resource URIs"""
    links = []
    for file in summary['files']:
        if file['url'].startswith(('http://', 'https://')):
            links.append(mcp_types.ResourceLink(name=file['name'], uri=file['url'], size=file['size']))
        if file.get('resource_uri') and file['name'].endswith('.md'):
            links.append(mcp_types.ResourceLink(name=file['name'], uri=file['resource_uri'], mime_type='text/markdown'))
    return links


def job_result(
    download_dir: Path, metadata: dict, base_url: str | None, is_error: bool = False, existing: bool = False
) -> ToolResult:
    summary = job_summary(download_dir, metadata, base_url, existing)
    content = [mcp_types.TextContent(text=summary_text(summary)), *resource_links(summary)]
    return ToolResult(content=content, structured_content=summary, is_error=is_error)


def change_key(metadata: dict) -> dict:
    """The metadata without the download progress, which changes every few seconds and would end every wait at once"""
    return {key: value for key, value in metadata.items() if key not in ('progress', 'updated_at')}


async def wait_for_change(download_dir: Path, metadata: dict, wait_seconds: float) -> dict:
    """The job's metadata once it changed, other than its progress, or the job is done, or after wait_seconds"""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    seen = change_key(metadata)
    while job_state(metadata)[0] not in TERMINAL_STATES:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(WAIT_POLL_SECONDS, remaining))
        metadata = load_metadata(download_dir) or metadata
        if change_key(metadata) != seen:
            break
    return metadata


def read_job_file(download_id: str, name: str) -> Path:
    try:
        download_dir = config.DOWNLOADS_DIR / str(uuid.UUID(download_id))
    except ValueError:
        raise ResourceError(f'Unknown job {download_id!r}.') from None
    path = download_dir / name
    if load_metadata(download_dir) is None or not path.is_file() or path.is_symlink():
        raise ResourceError(f'Job {download_id} has no {name}.')
    return path


def create_server(get_worker: Callable[[], JobWorker], settings: config.TranscriptionSettings) -> FastMCP:
    """The MCP server; get_worker returns the app's job worker, which the app's lifespan starts.

    Downloading and transcribing are always two steps: download_video, then transcribe_file when the user asks for a
    transcript or the subtitles are missing or broken. Without transcription configured there is no transcribe_file
    tool and no description mentions transcription, so an agent never sees an option that cannot work.
    """
    transcription_available = settings.available
    server = FastMCP('video-download', instructions=instructions(transcription_available))

    @server.tool(
        title='Download a video',
        description=download_video_description(transcription_available),
        output_schema=JOB_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    )
    async def download_video(url: VideoUrl, force: ForceDownload = False) -> ToolResult:
        try:
            submission = jobs.submit_download(get_worker(), url, force=force)
        except InvalidDownloadUrl as e:
            raise ToolError(str(e)) from e
        logger.info('Download %s %s over MCP', submission.download_id, 'found' if submission.existing else 'submitted')
        download_dir = config.DOWNLOADS_DIR / submission.download_id
        metadata = load_metadata(download_dir)
        return job_result(download_dir, metadata, request_base_url(), existing=submission.existing)

    @server.tool(
        title='Get a download job',
        description=get_job_description(transcription_available),
        output_schema=JOB_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def get_job(download_id: DownloadId, wait_seconds: WaitSeconds = 0) -> ToolResult:
        download_id, download_dir = job_dir(download_id)
        metadata = load_metadata(download_dir)
        if wait_seconds > 0:
            metadata = await wait_for_change(download_dir, metadata, min(wait_seconds, MAX_WAIT_SECONDS))
        return job_result(download_dir, metadata, request_base_url())

    if transcription_available:

        @server.tool(
            title='Transcribe a downloaded file',
            description=transcribe_file_description(settings),
            output_schema=JOB_SCHEMA,
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
        )
        async def transcribe_file(download_id: DownloadId, filename: MediaFilename) -> ToolResult:
            download_id, download_dir = job_dir(download_id)
            try:
                metadata = transcription.submit_transcription(get_worker(), download_id, filename)
            except transcription.TranscriptionRejected as e:
                raise ToolError(e.message) from None
            return job_result(download_dir, metadata, request_base_url())

    @server.resource(
        f'{RESOURCE_SCHEME}://jobs/{{download_id}}/metadata',
        mime_type='application/json',
        description='The metadata.json of a job: its state, the video details and the state of its transcriptions',
    )
    def job_metadata(download_id: str) -> ResourceResult:
        path = read_job_file(download_id, 'metadata.json')
        return ResourceResult([ResourceContent(path.read_text(encoding='utf-8'), mime_type='application/json')])

    @server.resource(
        f'{RESOURCE_SCHEME}://jobs/{{download_id}}/log',
        mime_type='text/plain',
        description="yt-dlp's log of a job's download",
    )
    def job_log(download_id: str) -> ResourceResult:
        path = read_job_file(download_id, LOG_FILENAME)
        return ResourceResult([ResourceContent(path.read_text(encoding='utf-8', errors='replace'))])

    @server.resource(
        f'{RESOURCE_SCHEME}://jobs/{{download_id}}/transcripts/{{filename}}',
        mime_type='text/markdown',
        description='A transcript of a job: <media>.transcript.md as Markdown, or <media>.transcript.json with segments',
    )
    def job_transcript(download_id: str, filename: str) -> ResourceResult:
        filename = unquote(filename)
        if not filename.endswith((transcription.TRANSCRIPT_MD_SUFFIX, transcription.TRANSCRIPT_JSON_SUFFIX)):
            raise ResourceError(f'{filename!r} is not a transcript.')
        path = read_job_file(download_id, filename)
        metadata = load_metadata(path.parent) or {}
        if filename not in {a.name for a in status_response(path.parent, metadata).artifacts if a.kind == TRANSCRIPT}:
            raise ResourceError(f'Job {download_id} has no transcript {filename!r}.')
        mime_type = 'text/markdown' if filename.endswith('.md') else 'application/json'
        return ResourceResult([ResourceContent(path.read_text(encoding='utf-8'), mime_type=mime_type)])

    return server
