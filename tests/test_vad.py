import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from video_download import vad

requires_ffmpeg = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg is not installed')

FRAME = vad.FRAME_SECONDS
PAD = vad.SPEECH_PAD_SECONDS


def frames(*runs: tuple[float, int]) -> list[float]:
    """Speech probabilities: (probability, number of frames) runs"""
    return [probability for probability, count in runs for _ in range(count)]


def test_speech_starts_above_the_threshold_and_ends_after_a_silence():
    probabilities = frames((0.1, 10), (0.9, 20), (0.1, 10), (0.8, 5))

    regions = vad.regions_from_probabilities(probabilities)

    assert regions == [
        (round(10 * FRAME - PAD, 3), round(30 * FRAME + PAD, 3)),
        (round(40 * FRAME - PAD, 3), round(45 * FRAME, 3)),
    ]


def test_a_dip_shorter_than_the_minimum_silence_does_not_end_speech():
    # 2 frames are 64 ms, under the 100 ms of MIN_SILENCE_SECONDS
    probabilities = frames((0.9, 10), (0.1, 2), (0.9, 10), (0.1, 20))

    assert vad.regions_from_probabilities(probabilities) == [(0.0, round(22 * FRAME + PAD, 3))]


def test_probabilities_between_the_thresholds_keep_speech_going():
    probabilities = frames((0.9, 10), (0.4, 30), (0.1, 20))

    assert vad.regions_from_probabilities(probabilities) == [(0.0, round(40 * FRAME + PAD, 3))]


def test_no_speech_is_no_region():
    assert vad.regions_from_probabilities(frames((0.2, 100))) == []
    assert vad.regions_from_probabilities([]) == []


def test_speech_is_what_lies_between_the_silences():
    assert vad.regions_from_silences([(2.0, 3.5), (0.0, 0.4), (7.0, 9.0)], 9.0) == [(0.4, 2.0), (3.5, 7.0)]
    assert vad.regions_from_silences([], 5.0) == [(0.0, 5.0)]


def tone_and_silence(path):
    """2 s of a tone, 1.5 s of silence, 2 s of a tone, as Opus like the extracted audio"""
    tone = 'sine=frequency=440:sample_rate=16000:duration=2'
    silence = 'anullsrc=channel_layout=mono:sample_rate=16000:d=1.5'
    subprocess.run(
        ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', tone, '-f', 'lavfi', '-i', silence,
         '-f', 'lavfi', '-i', tone, '-filter_complex', '[0][1][2]concat=n=3:v=0:a=1', '-ac', '1', '-c:a', 'libopus',
         '-b:a', '24k', str(path)],
        check=True,
        timeout=60,
    )  # fmt: skip
    return path


@requires_ffmpeg
def test_the_detector_runs_in_a_child_process_on_real_audio(tmp_path, monkeypatch):
    audio_path = tone_and_silence(tmp_path / 'tone.ogg')
    # The child process finds the package although the service runs from elsewhere
    monkeypatch.chdir(tmp_path)

    regions = asyncio.run(vad.detect_with_vad(audio_path.relative_to(tmp_path)))

    # A tone is not speech; what is checked is that Silero ran over the whole file and answered
    assert all(0 <= start < end <= 5.6 for start, end in regions)


@requires_ffmpeg
def test_the_detector_reads_the_whole_stream(tmp_path):
    audio_path = tone_and_silence(tmp_path / 'tone.ogg')

    probabilities = list(vad.speech_probabilities(audio_path))

    assert len(probabilities) == pytest.approx(5.5 / FRAME, abs=3)
    assert all(0 <= p <= 1 for p in probabilities)


@requires_ffmpeg
def test_silencedetect_finds_the_silence(tmp_path):
    audio_path = tone_and_silence(tmp_path / 'tone.ogg')

    regions = asyncio.run(vad.detect_with_silencedetect(audio_path, 5.5))

    (first_start, first_end), (second_start, second_end) = regions
    assert first_start == pytest.approx(0, abs=0.1)
    assert first_end == pytest.approx(2.0, abs=0.15)
    assert second_start == pytest.approx(3.5, abs=0.15)
    assert second_end == pytest.approx(5.5, abs=0.15)


def test_silencedetect_is_the_fallback_when_the_detector_fails(monkeypatch, caplog):
    async def broken(audio_path):
        raise vad.VadError('the voice activity detector exited with 1: ImportError')

    async def silences(audio_path, duration):
        return [(0.0, 2.0)]

    monkeypatch.setattr(vad, 'detect_with_vad', broken)
    monkeypatch.setattr(vad, 'detect_with_silencedetect', silences)

    speech = asyncio.run(vad.detect_speech(Path('a.ogg'), 10.0, logging.getLogger('test')))

    assert speech == vad.Speech([(0.0, 2.0)], 'silencedetect')
    assert 'falling back to silencedetect' in caplog.text


def test_without_any_detection_the_caller_cuts_blind(monkeypatch, caplog):
    async def broken(*args):
        raise vad.VadError('broken')

    monkeypatch.setattr(vad, 'detect_with_vad', broken)
    monkeypatch.setattr(vad, 'detect_with_silencedetect', broken)

    assert asyncio.run(vad.detect_speech(Path('a.ogg'), 10.0, logging.getLogger('test'))) is None
    assert 'cutting without pauses' in caplog.text


def test_main_prints_the_regions_as_json(monkeypatch, capsys):
    monkeypatch.setattr(vad, 'speech_probabilities', lambda path: frames((0.9, 31), (0.1, 10)))

    assert vad.main(['a.ogg']) == 0

    assert json.loads(capsys.readouterr().out) == {'regions': [[0.0, round(31 * FRAME + PAD, 3)]]}
