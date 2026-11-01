"""ffmpeg: the audio track of a media file, in the format speech models take"""

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Every speech model resamples to 16 kHz mono; Opus at 24 kbps is about 11 MB per hour of audio
AUDIO_EXTENSION = 'ogg'
AUDIO_MIME_TYPE = 'audio/ogg'
FFMPEG_AUDIO_ARGS = ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libopus', '-b:a', '24k', '-application', 'voip']

# Generous: ffmpeg decodes an hour of audio in seconds, a stuck process must not hold a worker slot forever
FFMPEG_TIMEOUT_SECONDS = 1800


class AudioExtractionError(Exception):
    pass


async def run(args: list[str], timeout: float, cwd: Path | None = None) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return process.returncode, stdout, stderr


async def write_audio(input_args: list[str], target: Path):
    """Run ffmpeg on input_args and write 16 kHz mono Opus to target; raises AudioExtractionError"""
    args = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *input_args, *FFMPEG_AUDIO_ARGS]
    try:
        code, _, stderr = await run([*args, str(target)], FFMPEG_TIMEOUT_SECONDS)
    except FileNotFoundError as e:
        raise AudioExtractionError('ffmpeg is not installed') from e
    except TimeoutError as e:
        raise AudioExtractionError(f'ffmpeg did not finish in {FFMPEG_TIMEOUT_SECONDS} s') from e
    if code != 0 or not target.is_file() or target.stat().st_size == 0:
        message = stderr.decode('utf-8', 'replace').strip().splitlines()[-3:]
        raise AudioExtractionError(f'ffmpeg exited with {code}: ' + (' '.join(message) or 'no audio was written'))


async def extract_audio(media: Path, target: Path):
    """Write the audio of media to target as 16 kHz mono Opus; raises AudioExtractionError"""
    await write_audio(['-i', str(media)], target)


async def cut_audio(source: Path, target: Path, start: float, end: float):
    """Write [start, end) of the audio to target as its own file starting at 0, re-encoded so the cut is exact;
    raises AudioExtractionError"""
    await write_audio(['-ss', f'{start:.3f}', '-to', f'{end:.3f}', '-i', str(source)], target)


async def probe_duration(path: Path) -> float | None:
    """Duration in seconds from ffprobe, None when it cannot tell"""
    args = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)]
    try:
        code, stdout, _ = await run(args, 60)
        if code != 0:
            return None
        duration = float(json.loads(stdout)['format']['duration'])
        return duration if duration > 0 else None
    except FileNotFoundError, TimeoutError, KeyError, ValueError, TypeError:
        return None
