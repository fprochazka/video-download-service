"""The attempts of one download: the configured options first, then a retry or a fallback chosen by the failure.

yt-dlp does not retry a 4xx, and it has no built-in "try another YouTube player client". Each attempt is a new yt-dlp
run with a full extraction, so it holds the worker slot and sends the site more requests from the same address; the list
stays short and stops at once on a failure that another attempt cannot fix, such as a rate limit.
"""

import asyncio
import dataclasses
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from video_download import config, storage
from video_download.errors import classify_error_safely
from video_download.progress import DownloadProgress
from video_download.ytdlp import (
    DownloadResult,
    build_ydl_opts,
    is_instagram_extractor,
    is_youtube_extractor,
    run_ytdlp,
)

logger = logging.getLogger(__name__)

# yt-dlp's own default: the best video and audio, any protocol, HLS included. On YouTube HLS needs no PO token.
BROAD_FORMAT = 'bv*+ba/b'

# A server error, a dropped connection or a timeout can pass, the same options are tried once more after a pause
TRANSIENT_CODES = ('network',)
# The site refused the media or offered no usable format: other options may get it
FALLBACK_CODES = ('forbidden', 'no_formats', 'format_unavailable')
# Once the fallbacks run, a failure of one client or format says little about the next one: a client can fail its own
# extraction ("The page needs to be reloaded." from tv). Only a rate limit, which the next attempt would hit too, ends
# the fallbacks early.
FALLBACK_STOP_CODES = ('rate_limited', 'interrupted')


@dataclass(frozen=True)
class Attempt:
    """What an attempt changes in the configured options; the first attempt changes nothing"""

    player_client: str | None = None
    broad_format: bool = False
    youtube_cookies: bool = False

    def changes(self) -> list[str]:
        changes = []
        if self.player_client:
            changes.append(f'player_client {self.player_client}')
        if self.broad_format:
            changes.append(f'format {BROAD_FORMAT}')
        if self.youtube_cookies:
            changes.append('youtube cookies')
        return changes


def is_fallback(attempt: Attempt) -> bool:
    return bool(attempt.player_client or attempt.broad_format)


def fallbacks(youtube: bool, ydl_opts: dict) -> list[Attempt]:
    """The attempts after a refused media file or a format failure, in order: one player client at a time on YouTube,
    then the broad format, which overrides the configured format for its attempt only"""
    attempts = []
    if youtube:
        configured = (ydl_opts.get('extractor_args') or {}).get('youtube', {}).get('player_client')
        for client in config.youtube_fallback_clients():
            if configured != [client]:
                attempts.append(Attempt(player_client=client))
    if ydl_opts.get('format') != BROAD_FORMAT:
        attempts.append(Attempt(broad_format=True))
    return attempts


def attempt_opts(ydl_opts: dict, attempt: Attempt, cookies: str | None = None) -> dict:
    """The options of one attempt. They are copies: the extractor args come from the module-level config, which
    concurrent jobs share."""
    opts = dict(ydl_opts)
    if attempt.player_client:
        extractor_args = dict(opts.get('extractor_args') or {})
        extractor_args['youtube'] = {**extractor_args.get('youtube', {}), 'player_client': [attempt.player_client]}
        opts['extractor_args'] = extractor_args
    if attempt.broad_format:
        opts['format'] = BROAD_FORMAT
    if attempt.youtube_cookies:
        # A text stream instead of the path, so yt-dlp never writes the cookies back to the file
        opts['cookiefile'] = io.StringIO(cookies or '')
    return opts


async def download(
    download_id: str,
    download_dir: Path,
    download_url: str,
    extractor: str | None,
    ytdlp_logger: logging.Logger,
    progress: DownloadProgress,
) -> DownloadResult:
    """Run the attempts until one downloads the media or none is left; every attempt goes into metadata.json.

    The result is the last attempt's, with two changes when it failed. When the fallbacks ran out, its error is the
    failure that started them, the refused media rather than a client that could not read the page, unless the last
    fallback hit a rate limit. When it failed before its extraction ended, it carries the info of the last attempt
    that extracted the video, so the job keeps those details and subtitles.
    """
    youtube = is_youtube_extractor(extractor)
    ydl_opts = build_ydl_opts(download_dir, ytdlp_logger, instagram_cookies=is_instagram_extractor(extractor))
    remaining = fallbacks(youtube, ydl_opts)
    if youtube:
        storage.update_metadata(download_dir, used_youtube_cookies=False)

    attempt = Attempt()
    retry = False
    cookies = None
    records = []
    extracted = None
    # (error, phase) of the attempt whose failure started the fallbacks
    cause = None
    while True:
        if records:
            progress.start_attempt()
        started_at = storage.now_iso()
        result = await asyncio.to_thread(run_ytdlp, attempt_opts(ydl_opts, attempt, cookies), download_url, progress)
        error = classify_error_safely(result.error, result.error_phase) if result.error is not None else None
        records.append(
            {
                'attempt': len(records) + 1,
                'changes': attempt.changes(),
                'retry': retry,
                'started_at': started_at,
                'ended_at': storage.now_iso(),
                **(error or {'error': None, 'error_code': None, 'error_type': None, 'error_phase': None}),
            }
        )
        storage.update_metadata(download_dir, attempts=records)
        if result.info is not None:
            extracted = result
        if error is None:
            return result

        code = error['error_code']
        pause = 0.0
        if code in TRANSIENT_CODES and not retry:
            retry = True
            pause = config.download_retry_pause()
        elif remaining and (code in FALLBACK_CODES or is_fallback(attempt) and code not in FALLBACK_STOP_CODES):
            if cause is None and not is_fallback(attempt):
                cause = (result.error, result.error_phase)
            attempt, retry = remaining.pop(0), False
        elif code == 'needs_auth' and youtube and cookies is None and (cookies_file := config.youtube_cookies_file()):
            # Only after YouTube asked for a login: the cookies are a Google account that YouTube can ban
            cookies = cookies_file.read_text(encoding='utf-8')
            attempt, retry = Attempt(youtube_cookies=True), False
            storage.update_metadata(download_dir, used_youtube_cookies=True)
        else:
            break

        if retry:
            plan = f'the same options again after {pause:g} s'
        else:
            plan = ', '.join(attempt.changes())
        logger.info('Download %s: attempt %d failed with %s, trying %s', download_id, len(records), code, plan)
        ytdlp_logger.info('Attempt %d failed with %s, trying %s', len(records), code, plan)
        if pause:
            await asyncio.sleep(pause)

    error, error_phase = result.error, result.error_phase
    if cause is not None and code not in FALLBACK_STOP_CODES:
        error, error_phase = cause
    reported = extracted if result.info is None and extracted is not None else result
    return dataclasses.replace(reported, error=error, error_phase=error_phase)
