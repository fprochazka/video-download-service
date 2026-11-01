"""Where a recording has speech, from the Silero voice activity detector, or from ffmpeg's silencedetect without it.

The transcription cuts long recordings inside pauses, so a cut never splits a word. Silero tells speech from music and
noise; silencedetect only measures the level, so music or a noisy room hides the pauses from it.

The detector runs in a child process (`python -m video_download.vad <audio>`): it is native code that keeps four
threads busy for about half a minute per hour and a half of audio, and a crash in it must not take the service down.
"""

import json
import re
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path

from video_download import audio

SAMPLE_RATE = 16000
# Silero takes windows of 512 samples at 16 kHz, 32 ms
FRAME_SAMPLES = 512
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

# Silero's own defaults: speech starts above THRESHOLD and ends after MIN_SILENCE_SECONDS below NEG_THRESHOLD
THRESHOLD = 0.5
NEG_THRESHOLD = 0.35
MIN_SILENCE_SECONDS = 0.1
# Added on both sides of each speech region, so a pause is never measured into the quiet start or end of a word
SPEECH_PAD_SECONDS = 0.1

SILENCE_NOISE = '-35dB'
SILENCE_MIN_SECONDS = 0.4

# An hour and a half took 45 s on a desktop CPU, a much slower server still fits
VAD_TIMEOUT_SECONDS = 1200

SILENCE_RE = re.compile(r'silence_(start|end): (-?[0-9.]+)')


class VadError(Exception):
    pass


@dataclass
class Speech:
    # (start, end) in seconds, sorted and not overlapping
    regions: list[tuple[float, float]]
    # vad or silencedetect
    source: str


def regions_from_probabilities(probabilities, frame_seconds: float = FRAME_SECONDS) -> list[tuple[float, float]]:
    """Speech regions from per-frame speech probabilities, with the hysteresis of Silero's get_speech_timestamps"""
    regions = []
    start = None
    silence_from = None
    min_silence_frames = max(1, round(MIN_SILENCE_SECONDS / frame_seconds))
    count = 0
    for index, probability in enumerate(probabilities):
        count = index + 1
        if probability >= THRESHOLD:
            silence_from = None
            if start is None:
                start = index
        elif probability < NEG_THRESHOLD and start is not None:
            if silence_from is None:
                silence_from = index
            if index + 1 - silence_from >= min_silence_frames:
                regions.append((start, silence_from))
                start = silence_from = None
    if start is not None:
        regions.append((start, silence_from if silence_from is not None else count))
    end_of_audio = count * frame_seconds
    return merge_overlaps(
        [
            (
                max(0.0, s * frame_seconds - SPEECH_PAD_SECONDS),
                min(end_of_audio, e * frame_seconds + SPEECH_PAD_SECONDS),
            )
            for s, e in regions
        ]
    )


def merge_overlaps(regions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(regions):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(round(s, 3), round(e, 3)) for s, e in merged]


def speech_probabilities(audio_path: Path):
    """Yield Silero's speech probability of each 32 ms window of the audio, decoded by ffmpeg to 16 kHz mono floats"""
    from pysilero_vad import SileroVoiceActivityDetector

    detector = SileroVoiceActivityDetector()
    frame_bytes = FRAME_SAMPLES * 4
    args = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(audio_path)]
    args += ['-f', 'f32le', '-ac', '1', '-ar', str(SAMPLE_RATE), '-']
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            data = process.stdout.read(frame_bytes * 1024)
            if not data:
                break
            # A short read at the end of the stream; the last partial window is padded with silence
            while len(data) % frame_bytes and (more := process.stdout.read(frame_bytes - len(data) % frame_bytes)):
                data += more
            if len(data) % frame_bytes:
                data += bytes(frame_bytes - len(data) % frame_bytes)
            samples = array('f', data)
            for offset in range(0, len(samples), FRAME_SAMPLES):
                yield detector.process_samples(samples[offset : offset + FRAME_SAMPLES].tolist())
        stderr = process.stderr.read().decode('utf-8', 'replace').strip()
        if process.wait() != 0:
            raise VadError(f'ffmpeg exited with {process.returncode}: {stderr[-300:]}')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def main(argv: list[str]) -> int:
    regions = regions_from_probabilities(speech_probabilities(Path(argv[0])))
    json.dump({'regions': regions}, sys.stdout)
    return 0


async def detect_with_vad(audio_path: Path) -> list[tuple[float, float]]:
    """Speech regions from Silero in a child process; raises VadError"""
    args = [sys.executable, '-m', 'video_download.vad', str(audio_path.resolve())]
    try:
        # From the directory that holds the package, the service may run from another one
        code, stdout, stderr = await audio.run(args, VAD_TIMEOUT_SECONDS, cwd=Path(__file__).resolve().parent.parent)
    except TimeoutError as e:
        raise VadError(f'the voice activity detector did not finish in {VAD_TIMEOUT_SECONDS} s') from e
    if code != 0:
        message = stderr.decode('utf-8', 'replace').strip().splitlines()[-1:]
        raise VadError(f'the voice activity detector exited with {code}: ' + ' '.join(message))
    try:
        return [(float(s), float(e)) for s, e in json.loads(stdout)['regions']]
    except (ValueError, KeyError, TypeError) as e:
        raise VadError(f'the voice activity detector answered {stdout[:200]!r}') from e


def regions_from_silences(silences: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    regions = []
    position = 0.0
    for start, end in sorted(silences):
        if start > position:
            regions.append((position, start))
        position = max(position, end)
    if duration > position:
        regions.append((position, duration))
    return merge_overlaps(regions)


async def detect_with_silencedetect(audio_path: Path, duration: float) -> list[tuple[float, float]]:
    """Speech regions as everything that ffmpeg's silencedetect does not call silence; raises VadError"""
    args = ['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-i', str(audio_path)]
    args += ['-af', f'silencedetect=noise={SILENCE_NOISE}:duration={SILENCE_MIN_SECONDS}', '-f', 'null', '-']
    try:
        code, _, stderr = await audio.run(args, audio.FFMPEG_TIMEOUT_SECONDS)
    except (FileNotFoundError, TimeoutError) as e:
        raise VadError(f'ffmpeg silencedetect failed: {type(e).__name__}') from e
    if code != 0:
        raise VadError(f'ffmpeg silencedetect exited with {code}')
    silences = []
    start = None
    for kind, value in SILENCE_RE.findall(stderr.decode('utf-8', 'replace')):
        if kind == 'start':
            start = max(0.0, float(value))
        elif start is not None:
            silences.append((start, float(value)))
            start = None
    if start is not None:
        silences.append((start, duration))
    return regions_from_silences(silences, duration)


async def detect_speech(audio_path: Path, duration: float, logger) -> Speech | None:
    """Speech regions from Silero, else from silencedetect, None when neither works; the caller then cuts blind"""
    try:
        return Speech(await detect_with_vad(audio_path), 'vad')
    except (VadError, OSError) as e:
        logger.warning('Voice activity detection of %s failed, falling back to silencedetect: %s', audio_path.name, e)
    try:
        return Speech(await detect_with_silencedetect(audio_path, duration), 'silencedetect')
    except VadError as e:
        logger.warning('silencedetect of %s failed, cutting without pauses: %s', audio_path.name, e)
    return None


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
