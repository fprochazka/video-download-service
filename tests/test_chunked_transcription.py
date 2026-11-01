import json
import math
import re

import pytest
from google.genai import errors

from tests.genai_fakes import response
from tests.test_transcription import JOB_ID, MEDIA, VTT, state_of, transcribe, write_job
from tests.ytdlp_fakes import read_metadata
from video_download import audio, transcription, vad

DURATION = 1200.0
# Speech everywhere but a pause of a second every 50 s, at 49.5 s, 99.5 s, ...
PAUSES = [(t - 0.5, t + 0.5) for t in range(50, 1200, 50)]
CHAPTERS = [(0, 'Intro'), (300, 'Engines'), (590, 'Wheels'), (900, 'Brakes')]


def speech_regions():
    regions, position = [], 0.0
    for start, end in PAUSES:
        regions.append((position, start))
        position = end
    return [*regions, (position, DURATION)]


@pytest.fixture
def long_audio(monkeypatch):
    """ffmpeg, ffprobe and Silero replaced: the media is 20 minutes long, a chunk's audio names its time range"""
    cuts = []
    detections = []

    async def extract(media, target):
        target.write_bytes(b'OggS whole')

    async def probe(path):
        return DURATION

    async def cut(source, target, start, end):
        cuts.append((start, end))
        target.write_bytes(f'OggS {start:.2f}-{end:.2f}'.encode())

    async def detect(audio_path, duration, logger):
        detections.append(audio_path.name)
        return vad.Speech(speech_regions(), 'vad')

    monkeypatch.setattr(audio, 'extract_audio', extract)
    monkeypatch.setattr(audio, 'probe_duration', probe)
    monkeypatch.setattr(audio, 'cut_audio', cut)
    monkeypatch.setattr(vad, 'detect_speech', detect)
    monkeypatch.setattr(transcription, 'TRANSIENT_RETRY_DELAYS', (0, 0))
    return {'cuts': cuts, 'detections': detections}


def chunk_range(contents) -> tuple[float, float]:
    start, end = re.fullmatch(rb'OggS ([\d.]+)-([\d.]+)', contents[0].inline_data.data).groups()
    return float(start), float(end)


def stamp(seconds: int) -> str:
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def lines_for(start: float, end: float, every: int = 10) -> str:
    """A transcript of the chunk: a line every 10 s, its text the absolute time, so the stitching is visible"""
    return '\n'.join(
        f'[{stamp(t)}] Speaker {1 + t // 20 % 2}: Words at {round(start) + t} here.'
        for t in range(0, math.ceil(end - start), every)
    )


def answer(start: float, end: float, text: str | None = None, **kwargs):
    seconds = end - start
    kwargs.setdefault('audio_tokens', math.ceil(seconds * 25))
    kwargs.setdefault('usage', (math.ceil(seconds * 25) + 1000, 100, 0))
    return response(text=lines_for(start, end) if text is None else text, **kwargs)


def good_answers(contents):
    return answer(*chunk_range(contents))


def long_job(downloads_dir, chapters=CHAPTERS, subtitles=None):
    job_dir = write_job(downloads_dir, duration=DURATION)
    info = {'id': 'abc', 'chapters': [{'start_time': t, 'title': title} for t, title in chapters]}
    (job_dir / 'info.json').write_text(json.dumps(info))
    if subtitles is not None:
        (job_dir / VTT).write_text(subtitles)
    return job_dir


def vtt(cues) -> str:
    """WEBVTT of (start seconds, text) cues, each 2 s long"""
    blocks = [f'{stamp(int(t))}.000 --> {stamp(int(t) + 2)}.000\n{text}' for t, text in cues]
    return 'WEBVTT\n\n' + '\n\n'.join(blocks) + '\n'


def test_a_long_recording_is_transcribed_in_chunks_cut_at_its_chapters(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir, subtitles=vtt([(t, f'Words at {t} here.') for t in range(0, 1200, 10)]))
    genai.answer = good_answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed', state.get('error')
    # Chapters at 300 and 590 merge into chunks of up to 5 minutes, each cut moves to the pause nearest the chapter
    assert long_audio['cuts'] == [(0.0, 300.0), (300.0, 600.0), (600.0, 900.0), (900.0, 1200.0)]
    assert len(long_audio['detections']) == 1
    # Each chunk went inline in its request, nothing was uploaded
    assert genai.uploads == []
    assert sorted(chunk_range(r['contents']) for r in genai.requests) == long_audio['cuts']

    by_start = {chunk_range(r['contents'])[0]: r for r in genai.requests}
    prompt = by_start[600.0]['contents'][1]
    assert prompt.startswith('Transcribe the attached audio. It is 00:05:00 (300 seconds) long.')
    # The chunk's own chapters, without times, and its own subtitles only
    assert '<video_chapters>\nWheels\n</video_chapters>' in prompt
    subtitles = prompt.split('<reference_subtitles>\n')[1].split('\n</reference_subtitles>')[0].splitlines()
    assert subtitles == [f'Words at {t} here.' for t in range(600, 900, 10)]
    assert by_start[600.0]['config'].max_output_tokens == 8192

    saved = json.loads((job_dir / 'Engine_Talk-abc.transcript.json').read_text())
    segments = saved['segments']
    assert len(segments) == 120
    # Timestamps are shifted by the chunk start, a segment ends where the next one starts, across the cuts too
    assert [s['start'] for s in segments] == list(range(0, 1200, 10))
    assert all(s['text'] == f'Words at {s["start"]} here.' for s in segments)
    assert segments[29] == {'start': 290, 'end': 300, 'speaker': 'Speaker 1', 'text': 'Words at 290 here.', 'chunk': 0}
    assert segments[30]['chunk'] == 1
    assert saved['speech_detection'] == 'vad'
    assert saved['chapters'][1] == {'start': 300.0, 'title': 'Engines'}
    chunk = saved['chunks'][2]
    assert {key: chunk[key] for key in ('index', 'start', 'end', 'cut', 'snap_distance', 'pause', 'chapters')} == {
        'index': 2,
        'start': 600.0,
        'end': 900.0,
        'cut': 'chapter',
        'snap_distance': 10.0,
        'pause': 1.0,
        'chapters': ['Wheels'],
    }
    assert chunk['checks'] == {
        'segments': 30,
        'speech_seconds': 294.0,
        'chars_per_speech_minute': chunk['checks']['chars_per_speech_minute'],
        'subtitle_recall': 1.0,
    }
    (attempt,) = chunk['attempts']
    assert attempt['usage'] == {
        'prompt_tokens': 8500,
        'output_tokens': 100,
        'thinking_tokens': 0,
        'prompt_audio_tokens': 7500,
    }
    assert attempt['problems'] == []
    assert attempt['cost']['usd'] == pytest.approx((8500 * 0.30 + 100 * 2.50) / 1e6)

    # The cost is the sum over the requests
    assert state['usage'] == {
        'prompt_tokens': 34000,
        'output_tokens': 400,
        'thinking_tokens': 0,
        'prompt_audio_tokens': 30000,
    }
    assert state['cost']['usd'] == pytest.approx((34000 * 0.30 + 400 * 2.50) / 1e6)
    assert state['warnings'] == []
    # metadata keeps each chunk's record, the segments are in the transcript
    assert [c['status'] for c in state['chunks']] == ['completed'] * 4
    assert 'segments' not in state['chunks'][0]
    assert not list(job_dir.glob('.*.ogg'))

    markdown = (job_dir / 'Engine_Talk-abc.transcript.md').read_text().splitlines()
    assert markdown[2].endswith(
        'The recording was transcribed in 4 parts, the speaker numbers start again in each part.'
    )
    headings = [line for line in markdown if line.startswith(('##', '*Part'))]
    assert headings == [
        '## Intro',
        '## Engines',
        '*Part 2 of 4, the speaker numbers start again.*',
        '## Wheels',
        '*Part 3 of 4, the speaker numbers start again.*',
        '## Brakes',
        '*Part 4 of 4, the speaker numbers start again.*',
    ]
    assert markdown[markdown.index('## Wheels') + 2] == '**[09:50] Speaker 1:** Words at 590 here.'


def test_without_chapters_each_part_has_a_heading(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir, chapters=[])
    genai.answer = good_answers

    transcribe(client)

    markdown = (job_dir / 'Engine_Talk-abc.transcript.md').read_text().splitlines()
    assert [line for line in markdown if line.startswith('##')] == [f'## Part {n} of 4' for n in range(1, 5)]
    assert [c['cut'] for c in state_of(downloads_dir)['chunks']] == ['start', 'pause', 'pause', 'pause']


def test_a_recording_under_7_minutes_is_still_one_uploaded_request(
    client, downloads_dir, genai, long_audio, monkeypatch
):
    async def almost_7_minutes(path):
        return 419.0

    monkeypatch.setattr(audio, 'probe_duration', almost_7_minutes)
    long_job(downloads_dir)

    transcribe(client)

    assert state_of(downloads_dir)['status'] == 'completed'
    assert len(genai.uploads) == 1
    assert len(genai.requests) == 1
    assert long_audio['cuts'] == []
    assert long_audio['detections'] == []
    assert 'chunks' not in state_of(downloads_dir)


def test_an_answer_that_fails_the_checks_is_sent_again_and_both_are_paid(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir)
    sent = []

    def answers(contents):
        start, end = chunk_range(contents)
        sent.append(start)
        if start == 300.0 and sent.count(start) == 1:
            # Stopped early: nothing after the first minute of a 5-minute chunk
            return answer(start, end, lines_for(start, start + 60))
        return answer(start, end)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert sent.count(300.0) == 2
    attempts = state['chunks'][1]['attempts']
    assert [a['problems'][0]['code'] if a['problems'] else None for a in attempts] == ['stopped_early', None]
    assert '245 s of speech after the last transcript line' in attempts[0]['problems'][0]['message']
    assert state['usage']['prompt_tokens'] == 34000 + 8500
    saved = json.loads((job_dir / 'Engine_Talk-abc.transcript.json').read_text())
    assert len(saved['segments']) == 120


HELLO = '[00:00:01] Speaker 1: Hello.'


@pytest.mark.parametrize(
    ('broken', 'code'),
    [
        (answer(300, 600, HELLO, finish_reason='MAX_TOKENS'), 'output_truncated'),
        (answer(300, 600, '[00:00:01] Speaker 1: We went to the shop.\n' * 10), 'repeated_text'),
        (
            answer(
                300,
                600,
                '[00:00:30] Speaker 1: Early.\n' + ''.join(f'[00:2{m}:00] Speaker 1: Late.\n' for m in range(5)),
            ),
            'beyond_chunk',
        ),
        (answer(300, 600, HELLO, audio_tokens=1000), 'audio_tokens'),
        (answer(300, 600, HELLO, usage=(0, 100, 0), audio_tokens=0), 'no_prompt_tokens'),
        (answer(300, 600, HELLO), 'stopped_early'),
        # A passage skipped in the middle
        (
            answer(
                300,
                600,
                '\n'.join(f'[{stamp(t)}] Speaker 1: Words.' for t in [*range(0, 100, 10), *range(200, 300, 10)]),
            ),
            'blind_gap',
        ),
        (answer(300, 600, 'I cannot transcribe this.'), 'invalid_response'),
    ],
)
def test_a_chunk_that_fails_twice_is_split_in_two(client, downloads_dir, genai, long_audio, broken, code):
    long_job(downloads_dir)

    def answers(contents):
        start, end = chunk_range(contents)
        return broken if (start, end) == (300.0, 600.0) else answer(start, end)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    chunk = state['chunks'][1]
    # Split at the longest pause in its middle half
    assert chunk['split_at'] == 450.0
    assert [(a['start'], a['end']) for a in chunk['attempts']] == [
        (300.0, 600.0),
        (300.0, 600.0),
        (300.0, 450.0),
        (450.0, 600.0),
    ]
    assert code in {p['code'] for p in chunk['attempts'][0]['problems']}
    assert len(genai.requests) == 3 + 4


def test_an_unrecoverable_chunk_fails_the_transcription_and_keeps_the_good_chunks(
    client, downloads_dir, genai, long_audio
):
    job_dir = long_job(downloads_dir)

    def answers(contents):
        start, end = chunk_range(contents)
        return response(text='', usage=(1000, 10, 0), audio_tokens=1) if 300 <= start < 600 else answer(start, end)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'failed'
    assert state['error_code'] == 'chunk_failed'
    assert state['error'].startswith('Part 2 of 4 (00:05:00 to 00:10:00) failed after 4 attempts:')
    assert not list(job_dir.glob('*.transcript.*'))
    chunks = state['chunks']
    assert [c['status'] for c in chunks] == ['completed', 'failed', 'completed', 'completed']
    # The good chunks keep their segments, so a retry would not need to send them again
    assert [len(c.get('segments') or []) for c in chunks] == [30, 0, 30, 30]
    assert chunks[2]['segments'][0]['start'] == 600
    # Every answered request is paid for, the failed ones too
    assert state['usage']['prompt_tokens'] == 3 * 8500 + 4 * 1000
    assert state['cost']['usd'] > 0


def test_transient_errors_are_sent_again(client, downloads_dir, genai, long_audio):
    long_job(downloads_dir)
    failures = []

    def answers(contents):
        start, end = chunk_range(contents)
        if start == 0.0 and len(failures) < 2:
            failures.append(start)
            return errors.ServerError(503, {'error': {'code': 503, 'message': 'Overloaded', 'status': 'UNAVAILABLE'}})
        return answer(start, end)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert len(state['chunks'][0]['attempts']) == 1
    assert len(genai.requests) == 4 + 2


def test_a_bad_request_fails_at_once(client, downloads_dir, genai, long_audio):
    long_job(downloads_dir)
    genai.answer = errors.ClientError(400, {'error': {'code': 400, 'message': 'Bad', 'status': 'INVALID_ARGUMENT'}})

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'failed'
    assert state['error_code'] == 'provider_error'
    # One request per chunk under the concurrency limit, none sent twice
    assert len(genai.requests) <= 4


def test_out_of_line_timestamps_are_repaired(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir)

    def answers(contents):
        start, end = chunk_range(contents)
        text = lines_for(start, end)
        if start == 0.0:
            # An hour digit where there is none
            text = text.replace('[00:01:10]', '[01:01:10]')
        return answer(start, end, text)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert state['chunks'][0]['attempts'][0]['repaired_timestamps'] == 0
    saved = json.loads((job_dir / 'Engine_Talk-abc.transcript.json').read_text())
    assert saved['segments'][7]['start'] == 70


def test_an_answer_written_a_minute_late_is_moved_back(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir)

    def answers(contents):
        start, end = chunk_range(contents)
        text = lines_for(start, end)
        if start == 900.0:
            text = '\n'.join(f'[{stamp(60 + t)}] Speaker 1: Words at {900 + t} here.' for t in range(0, 300, 10))
        return answer(start, end, text)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    (attempt,) = state['chunks'][3]['attempts']
    assert (attempt['minutes_late'], attempt['problems']) == (1, [])
    saved = json.loads((job_dir / 'Engine_Talk-abc.transcript.json').read_text())
    assert [s['start'] for s in saved['segments'][90:93]] == [900, 910, 920]


def test_the_lines_after_a_jump_in_the_timestamps_are_moved_back(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir)

    def answers(contents):
        start, end = chunk_range(contents)
        text = lines_for(start, end)
        if start == 900.0:
            # [00:01:00] written as [00:10:00], and every line after it 9 minutes late as well
            text = '\n'.join(
                f'[{stamp(t if t < 60 else t + 540)}] Speaker 1: Words at {900 + t} here.' for t in range(0, 300, 10)
            )
        return answer(start, end, text)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    (attempt,) = state['chunks'][3]['attempts']
    assert (attempt['jump_minutes'], attempt['minutes_late'], attempt['problems']) == (9, 0, [])
    saved = json.loads((job_dir / 'Engine_Talk-abc.transcript.json').read_text())
    assert [s['start'] for s in saved['segments'][95:98]] == [950, 960, 970]


def test_a_chunk_missing_subtitle_words_is_sent_again_and_flagged(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir, subtitles=vtt([(t, f'Words at {t} here.') for t in range(0, 1200, 5)]))
    sent = []

    def answers(contents):
        start, end = chunk_range(contents)
        sent.append(start)
        if start == 600.0:
            # Every other subtitle line is missing: half the words
            return answer(start, end, lines_for(start, end).replace('Speaker 2: Words', 'Speaker 2: Other'))
        return answer(start, end, lines_for(start, end, every=5))

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert sent.count(600.0) == 2
    assert [w['code'] for w in state['warnings']] == ['chunk_suspect']
    assert state['warnings'][0]['message'].startswith('Part 3 (00:10:00 to 00:15:00) may be missing speech:')
    assert state['chunks'][2]['suspect']
    assert len(state['chunks'][2]['attempts']) == 2
    assert (job_dir / 'Engine_Talk-abc.transcript.md').exists()


def test_without_speech_detection_the_cuts_are_hard(client, downloads_dir, genai, long_audio, monkeypatch):
    async def nothing(audio_path, duration, logger):
        return None

    monkeypatch.setattr(vad, 'detect_speech', nothing)
    long_job(downloads_dir, chapters=[])

    def answers(contents):
        start, end = chunk_range(contents)
        # Nothing after the first minute: without speech regions there is no gap check
        return answer(start, end, lines_for(start, start + 60))

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    assert state['speech_detection'] is None
    assert [c['cut'] for c in state['chunks']] == ['start', 'hard', 'hard', 'hard']
    # The checks of the whole transcript still see the missing end
    assert [w['code'] for w in state['warnings']] == ['ends_early']
    assert read_metadata(downloads_dir, JOB_ID)['transcriptions'][MEDIA]['chunks'][1]['start'] == 300.0


def test_an_answer_in_another_script_fails_the_check_and_is_sent_again(client, downloads_dir, genai, long_audio):
    job_dir = long_job(downloads_dir)
    sent = []

    def answers(contents):
        start, end = chunk_range(contents)
        sent.append(start)
        text = lines_for(start, end)
        if start == 300.0 and sent.count(start) == 1:
            # Transliterated into Cyrillic instead of the expected English
            text = text.replace('Words at', 'Слова при').replace('here', 'здесь')
        return answer(start, end, text)

    genai.answer = answers

    transcribe(client)

    state = state_of(downloads_dir)
    assert state['status'] == 'completed'
    attempts = state['chunks'][1]['attempts']
    assert [p['code'] for p in attempts[0]['problems']] == ['wrong_script']
    assert 'English (en) is not written in' in attempts[0]['problems'][0]['message']
    assert attempts[1]['problems'] == []
    assert 'Слова' not in (job_dir / 'Engine_Talk-abc.transcript.md').read_text()
