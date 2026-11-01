"""The MCP server at /mcp: a tool to download a video, tools to follow and transcribe its job, the job's text as resources.

Every tool works on the same jobs as the web page, through jobs.submit_download and transcription.submit_transcription,
and reads their state from metadata.json. download_video answers at once with the job's id, the agent follows the job
with get_job, which can wait for the next change.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools import ToolResult
from mcp_types import ToolAnnotations

from video_download import config, jobs, transcription
from video_download.status import status_response
from video_download.storage import LOG_FILENAME, TRANSCRIPT, load_metadata
from video_download.urls import InvalidDownloadUrl
from video_download.worker import JobWorker

logger = logging.getLogger(__name__)

RESOURCE_SCHEME = 'video-download'

# get_job checks metadata.json this often while it waits, and waits at most this long
WAIT_POLL_SECONDS = 0.5
MAX_WAIT_SECONDS = 60

TERMINAL_STATES = ('completed', 'failed')

INSTRUCTIONS = """Downloads videos from YouTube, Instagram and the other sites yt-dlp supports, and transcribes them with \
Gemini. Call download_video with the video's URL, set transcribe to true to get a transcript of every downloaded media \
file. A download takes from seconds to many minutes: when download_video answers with a job instead of the final \
result, call get_job with the job's download_id and wait_seconds=60 until its state is completed or failed. A \
completed job lists its files with URLs to fetch them. The transcript text is also readable as the resource named in \
the job's transcriptions."""


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
        return 'downloading', 'downloading'
    if status == 'failed':
        return 'failed', f'failed ({metadata.get("error_code") or "unknown"}): {metadata.get("error") or ""}'.strip()
    states = [state or {} for state in (metadata.get('transcriptions') or {}).values()]
    active = [state for state in states if state.get('status') in transcription.ACTIVE_TRANSCRIPTION_STATUSES]
    if active:
        current = min(len(states) - len(active) + 1, len(states))
        return 'transcribing', f'transcribing {current}/{len(states)}'
    return 'completed', 'completed'


def format_size(size: int | None) -> str:
    if size is None:
        return 'size unknown'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return ''


def job_summary(download_dir: Path, metadata: dict, base_url: str | None) -> dict:
    """The job as get_job returns it: state, video details, files with URLs, transcriptions with cost, resource URIs"""
    download_id = metadata['download_id']
    status = status_response(download_dir, metadata)
    state, message = job_state(metadata)
    base = base_url or ''
    files = []
    for entry in status.artifacts:
        file = {
            'name': entry.name,
            'kind': entry.kind,
            'size': entry.size,
            'url': f'{base}/downloads/{download_id}/{quote(entry.name)}',
        }
        if entry.media:
            file['media'] = entry.media
        if entry.lang:
            file['lang'] = entry.lang
            file['auto_captions'] = bool(entry.auto)
        if entry.kind == TRANSCRIPT:
            file['resource_uri'] = resource_uri(download_id, 'transcripts', entry.name)
        files.append(file)
    transcriptions = {}
    for media_name, item in status.transcriptions.items():
        transcriptions[media_name] = {
            'status': item.status,
            'model': item.model,
            'error': item.error,
            'error_code': item.error_code,
            'warnings': [warning.message for warning in item.warnings],
            'cost_usd': item.cost.usd if item.cost else None,
            'cost_estimated': item.cost.estimated if item.cost else None,
            'files': item.files,
            'transcript_resource_uri': next(
                (resource_uri(download_id, 'transcripts', name) for name in item.files if name.endswith('.md')), None
            ),
        }
    return {
        'download_id': download_id,
        'state': state,
        'done': state in TERMINAL_STATES,
        'message': message,
        'url': status.url,
        'job_url': f'{base}/jobs/{download_id}',
        'title': status.title,
        'duration': status.duration,
        'uploader': status.uploader,
        'error': status.error,
        'error_code': status.error_code,
        'auto_transcribe': status.auto_transcribe,
        'transcription_available': status.transcription_available,
        'files': files,
        'transcriptions': transcriptions,
        'resources': {
            'metadata': resource_uri(download_id, 'metadata'),
            'log': resource_uri(download_id, 'log'),
        },
        'created_at': status.created_at,
        'updated_at': status.updated_at,
    }


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
        if item['cost_usd'] is not None:
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


def job_result(download_dir: Path, metadata: dict, base_url: str | None, is_error: bool = False) -> ToolResult:
    summary = job_summary(download_dir, metadata, base_url)
    content = [mcp_types.TextContent(text=summary_text(summary)), *resource_links(summary)]
    return ToolResult(content=content, structured_content=summary, is_error=is_error)


async def wait_for_change(download_dir: Path, metadata: dict, wait_seconds: float) -> dict:
    """The job's metadata once it changed or the job is done, or after wait_seconds"""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    seen = metadata.get('updated_at')
    while job_state(metadata)[0] not in TERMINAL_STATES:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(WAIT_POLL_SECONDS, remaining))
        metadata = load_metadata(download_dir) or metadata
        if metadata.get('updated_at') != seen:
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


def create_server(get_worker: Callable[[], JobWorker]) -> FastMCP:
    """The MCP server; get_worker returns the app's job worker, which the app's lifespan starts"""
    server = FastMCP('video-download', instructions=INSTRUCTIONS)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    async def download_video(url: str, transcribe: bool = False) -> ToolResult:
        """Download the video at the URL with yt-dlp, with its subtitles and info.json.

        transcribe: also transcribe every downloaded media file with Gemini once the download completes.
        Answers at once with the job, follow it with get_job until its state is completed or failed.
        """
        try:
            download_id = jobs.submit_download(get_worker(), url, auto_transcribe=transcribe)
        except InvalidDownloadUrl as e:
            raise ToolError(str(e)) from e
        logger.info('Download %s submitted over MCP: transcribe=%s', download_id, transcribe)
        download_dir = config.DOWNLOADS_DIR / download_id
        return job_result(download_dir, load_metadata(download_dir), request_base_url())

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def get_job(download_id: str, wait_seconds: int = 0) -> ToolResult:
        """The state of a job: pending, downloading, transcribing, completed or failed, with its files and transcripts.

        wait_seconds (at most 60): wait until the job changes or is done before answering, instead of answering at once.
        """
        download_id, download_dir = job_dir(download_id)
        metadata = load_metadata(download_dir)
        if wait_seconds > 0:
            metadata = await wait_for_change(download_dir, metadata, min(wait_seconds, MAX_WAIT_SECONDS))
        return job_result(download_dir, metadata, request_base_url())

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    async def transcribe_file(download_id: str, filename: str) -> ToolResult:
        """Transcribe one media file of a completed job with Gemini, replacing an earlier transcript of it.

        filename is the name of a file of kind media from get_job. Follow the transcription with get_job.
        """
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
