"""ElevenLabs Scribe as a transcription provider: one speech-to-text request for the whole recording, and the words of
its answer turned into the segments of a transcript.

Scribe computes word timestamps and keeps one speaker label per person across the whole file, so a recording is never
cut into chunks. See docs/transcription-models.md for how it compares with Gemini.
"""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from text_to_num import alpha2digit

from video_download.gemini import ProviderError

API_URL = 'https://api.elevenlabs.io/v1/speech-to-text'
# The documented limits of one file are 10 hours and 3 GB; the extracted Opus audio is about 11 MB per hour, so only
# the length can be over
MAX_AUDIO_SECONDS = 10 * 3600
# Scribe answered 91 minutes of audio in 64 s and 64 minutes in 181 s
TIMEOUT = httpx.Timeout(30 * 60, connect=30)

TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
TOO_LONG_CODES = frozenset({'request_too_large', 'file_too_large', 'audio_too_long'})


def http_client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={'xi-api-key': api_key}, timeout=TIMEOUT)


@dataclass
class ScribeAnswer:
    # The JSON of the answer
    body: dict
    # What ElevenLabs reported about the request in the headers and the body, see provider_report
    report: dict = field(default_factory=dict)


def number(value: str | None) -> float | None:
    """A header's finite, non-negative number, None when it is missing or anything else"""
    try:
        result = float(value) if value is not None else None
    except ValueError:
        return None
    return result if result is not None and math.isfinite(result) and result >= 0 else None


def provider_report(headers: httpx.Headers, body: dict) -> dict:
    """What ElevenLabs reported about one answered request: its cost and credits, the ids to quote to its support, and
    the audio length it billed; the keys it did not report are left out.

    fiat-cost-before-overages is the amount of money of the request, before what a plan's overage pricing adds, and
    character-cost the credits it took from the plan. The response carries no other price information."""
    report = {
        'transcription_id': headers.get('transcription-id') or body.get('transcription_id'),
        'audio_seconds': body.get('audio_duration_secs'),
        'credits': number(headers.get('character-cost')),
        'region': headers.get('x-region'),
        'trace_id': headers.get('x-trace-id'),
    }
    amount = number(headers.get('fiat-cost-before-overages'))
    currency = (headers.get('fiat-currency') or '').strip().lower()
    if amount is not None and currency:
        report['cost'] = {'amount': amount, 'currency': currency}
    if report['credits'] is not None and report['credits'].is_integer():
        report['credits'] = int(report['credits'])
    if not isinstance(report['audio_seconds'], int | float):
        report['audio_seconds'] = None
    return {key: value for key, value in report.items() if value is not None}


@dataclass
class ElevenLabsClient:
    api_key: str
    model: str

    def form(self, language: str | None) -> dict:
        """The request's fields besides the file; numbers come back as words, the API has no option for digits"""
        fields = {
            'model_id': self.model,
            'diarize': 'true',
            'timestamps_granularity': 'word',
            'tag_audio_events': 'true',
        }
        if language:
            # ISO 639-1 or 639-3; the answer names the language in 639-3 (eng, ces) either way
            fields['language_code'] = language
        return fields

    async def transcribe(self, audio: Path, mime_type: str, language: str | None) -> ScribeAnswer:
        """The answer and its report; raises ProviderError with an error_code of the transcription"""
        try:
            async with http_client(self.api_key) as client:
                with audio.open('rb') as file:
                    response = await client.post(
                        API_URL, data=self.form(language), files={'file': (audio.name, file, mime_type)}
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderError('provider_error', f'Could not reach ElevenLabs: {type(e).__name__}', True) from e
        except httpx.TimeoutException as e:
            # Not sent again: the audio may have been transcribed and billed already
            message = f'ElevenLabs did not answer within {TIMEOUT.read / 60:.0f} minutes'
            raise ProviderError('provider_error', message) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                'provider_error', f'The request to ElevenLabs failed: {type(e).__name__}: {e}'[:500]
            ) from e
        except OSError as e:
            raise ProviderError('provider_error', f'Could not read the audio: {e}'[:500]) from e
        if response.status_code != 200:
            raise api_error(response)
        try:
            answer = response.json()
        except ValueError as e:
            raise ProviderError('invalid_response', 'ElevenLabs answered with something other than JSON.') from e
        if not isinstance(answer, dict) or not isinstance(answer.get('words'), list):
            raise ProviderError('invalid_response', 'The answer of ElevenLabs has no words.')
        return ScribeAnswer(answer, provider_report(response.headers, answer))


def error_detail(response: httpx.Response) -> tuple[str | None, str]:
    """The code and message of an error answer: {"detail": {"code", "message"}}, or a 422's list of fields"""
    try:
        detail = response.json().get('detail')
    except ValueError, AttributeError:
        return None, response.text[:300]
    if isinstance(detail, dict):
        return detail.get('code') or detail.get('status'), str(detail.get('message') or '')
    if isinstance(detail, list):
        fields = [f'{".".join(map(str, d.get("loc") or []))}: {d.get("msg")}' for d in detail if isinstance(d, dict)]
        return 'validation_error', '; '.join(fields)
    return None, str(detail or '')


def api_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    code, message = error_detail(response)
    text = f'ElevenLabs API error {status}' + (f' {code}' if code else '') + (f': {message}' if message else '')
    if status == 401:
        return ProviderError('not_configured', f'ElevenLabs rejected ELEVENLABS_API_KEY. {text}'[:1000])
    if status == 402:
        return ProviderError('insufficient_credits', text[:1000])
    if status == 413 or code in TOO_LONG_CODES:
        return ProviderError('too_long', text[:1000])
    if status == 429:
        return ProviderError('rate_limited', text[:1000], transient=True)
    return ProviderError('provider_error', text[:1000], transient=status in TRANSIENT_STATUS_CODES)


# --- The words of the answer as segments

# A segment ends after a word that ends a sentence, or after this long
SENTENCE_END_RE = re.compile(r'[.?!…]["”’»)\]]*$')
MAX_SEGMENT_SECONDS = 30
AUDIO_EVENT_RE = re.compile(r'\[[^\]]*\]')

# A turn of at most this many words and this long, between two turns of one other speaker of at least
# BACKCHANNEL_MIN_SURROUNDING_WORDS each, is an interjection such as "Mm-hmm", "Yeah" or "Jo"; the Markdown shows it in
# parentheses inside the other speaker's paragraph. A question, or the answer to one, stays a turn of its own. Without
# the last two conditions a 6-minute comedy dialogue lost 29 of its short replies ("No.", "What?") to the rule, with
# them 4; a 91-minute interview folds 96 turns, against 226 without them, mostly "Mm-hmm" and "Yeah".
BACKCHANNEL_MAX_WORDS = 2
BACKCHANNEL_MAX_SECONDS = 1.5
BACKCHANNEL_MIN_SURROUNDING_WORDS = 10


def speaker_labels(words: list[dict]) -> dict[str | None, str]:
    """Scribe's speaker ids as Speaker 1, 2, ... in the order they first speak; an id that has only audio events, such
    as the music of an intro, comes after them"""
    order = [w.get('speaker_id') for w in words if w.get('type') == 'word']
    order += [w.get('speaker_id') for w in words if w.get('type') == 'audio_event']
    ids = list(dict.fromkeys(order))
    return {speaker_id: f'Speaker {index}' for index, speaker_id in enumerate(ids, start=1)}


def segments(words: list[dict], language: str | None) -> list[dict]:
    """{start, end, speaker, text} per sentence of one speaker, times in seconds; audio events stay in the text"""
    labels = speaker_labels(words)
    result: list[dict] = []
    current = None
    closed = False
    for word in words:
        kind, text = word.get('type'), word.get('text') or ''
        if kind == 'spacing' or not text.strip():
            continue
        start = word.get('start')
        start = float(start) if start is not None else (current['end'] if current else 0.0)
        end = float(word.get('end') if word.get('end') is not None else start)
        speaker = labels.get(word.get('speaker_id'), 'Speaker 1')
        if current is None or speaker != current['speaker'] or closed or start - current['start'] > MAX_SEGMENT_SECONDS:
            current = {'start': start, 'end': end, 'speaker': speaker, 'text': text.strip()}
            result.append(current)
        else:
            current['text'] += ' ' + text.strip()
            current['end'] = max(current['end'], end)
        closed = kind == 'word' and bool(SENTENCE_END_RE.search(text.strip()))
    for segment in result:
        segment['start'], segment['end'] = round(segment['start'], 2), round(segment['end'], 2)
        segment['text'] = numbers_as_digits(segment['text'], language)
    return result


def spoken_words(text: str) -> int:
    return len(AUDIO_EVENT_RE.sub(' ', text).split())


def fold_backchannels(segments: list[dict]) -> int:
    """Mark the segments of each interjection with backchannel=True; returns how many turns were marked"""
    turns: list[list[dict]] = []
    for segment in segments:
        if turns and turns[-1][0]['speaker'] == segment['speaker']:
            turns[-1].append(segment)
        else:
            turns.append([segment])
    folded = 0
    for before, turn, after in zip(turns, turns[1:], turns[2:], strict=False):
        speaker = turn[0]['speaker']
        if before[0]['speaker'] != after[0]['speaker'] or before[0]['speaker'] == speaker:
            continue
        if sum(spoken_words(s['text']) for s in turn) > BACKCHANNEL_MAX_WORDS:
            continue
        if turn[-1]['end'] - turn[0]['start'] >= BACKCHANNEL_MAX_SECONDS:
            continue
        if any('?' in s['text'] for s in turn) or before[-1]['text'].rstrip().endswith('?'):
            continue
        if (
            min(sum(spoken_words(s['text']) for s in side) for side in (before, after))
            < BACKCHANNEL_MIN_SURROUNDING_WORDS
        ):
            continue
        for segment in turn:
            segment['backchannel'] = True
        folded += 1
    return folded


# Digits of a number of 10,000 or more, which alpha2digit writes without separators; a year stays as it is
LARGE_NUMBER_RE = re.compile(r'(?<![\d.,])\d{5,}(?!\d)')
# alpha2digit turns "a few million" into "a few 1000000", "$200 million" into "$200 1000000" and "a hundred people"
# into "a 100 people"; these words stay as they are
SCALE_RE = re.compile(
    r'\b(?:[Mm]illion|[Bb]illion|[Tt]rillion)(?:s|aires?)?\b|\b[Aa] (?:hundred|thousand)\b|\b(?:hundred|thousand)ths?\b'
)
# Private-use characters, which no transcript contains, hold the scale words while alpha2digit runs
PLACEHOLDER = '\ue000'
ENGLISH = ('en', 'eng')


def numbers_as_digits(text: str, language: str | None) -> str:
    """English numbers written as words in digits: "two thousand and twenty two" is 2022, "five million" 5 million.
    text2num has no Czech, so the text of any other language stays as Scribe wrote it."""
    if (language or '').lower() not in ENGLISH:
        return text
    scales = SCALE_RE.findall(text)
    converted = alpha2digit(SCALE_RE.sub(PLACEHOLDER, text.replace(PLACEHOLDER, '')), 'en')
    converted = LARGE_NUMBER_RE.sub(lambda match: f'{int(match[0]):,}', converted)
    parts = converted.split(PLACEHOLDER)
    if len(parts) != len(scales) + 1:
        return text
    return ''.join(part + scale for part, scale in zip(parts, [*scales, ''], strict=True))
