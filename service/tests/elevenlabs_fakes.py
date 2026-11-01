"""A stand-in for the ElevenLabs speech-to-text endpoint: records what the service sent, answers what the test set"""

import re

import httpx

API_KEY = 'test-elevenlabs-key-0123456789'

# (start, end, speaker_id, text); an audio event is a text in brackets
DEFAULT_WORDS = [
    (0.5, 0.9, 'speaker_1', 'Hello'),
    (1.0, 1.4, 'speaker_1', 'and'),
    (1.5, 2.1, 'speaker_1', 'welcome.'),
    (2.4, 2.8, 'speaker_0', 'Thanks'),
    (2.9, 3.3, 'speaker_0', 'for'),
    (3.4, 3.9, 'speaker_0', 'having'),
    (4.0, 4.5, 'speaker_0', 'me.'),
]


def words(entries=None) -> list[dict]:
    """Scribe's words, with the spacing items it puts between them"""
    result = []
    for start, end, speaker_id, text in DEFAULT_WORDS if entries is None else entries:
        if result:
            gap_start = result[-1]['end']
            result.append(
                {
                    'text': ' ',
                    'start': gap_start,
                    'end': start,
                    'type': 'spacing',
                    'speaker_id': speaker_id,
                    'logprob': 0,
                }
            )
        kind = 'audio_event' if text.startswith('[') else 'word'
        result.append(
            {'text': text, 'start': start, 'end': end, 'type': kind, 'speaker_id': speaker_id, 'logprob': -0.01}
        )
    return result


def answer(entries=None, language='eng', duration=15.0) -> dict:
    items = words(entries)
    return {
        'language_code': language,
        'language_probability': 0.98,
        'text': ''.join(item['text'] for item in items),
        'words': items,
        'transcription_id': 'tr-123',
        'audio_duration_secs': duration,
    }


# The headers of a real answer for 5 s of audio, its ids replaced
HEADERS = {
    'transcription-id': 'tr-123',
    'character-cost': '1',
    'fiat-cost-before-overages': '0.000364',
    'fiat-currency': 'usd',
    'current-concurrent-requests': '1',
    'workspace-concurrent-requests': '1',
    'maximum-concurrent-requests': '12',
    'x-trace-id': 'trace-456',
    'x-region': 'us-central1',
}


FIELD_RE = re.compile(
    rb'name="([^"]+)"(?:; filename="([^"]*)")?\r\n(?:Content-Type: ([^\r]*)\r\n)?\r\n(.*?)\r\n--', re.S
)


class FakeScribe:
    def __init__(self):
        self.requests: list[dict] = []
        # A dict is the JSON of a 200, an httpx.Response is sent as it is, an exception is raised; a list is answered
        # one item per request
        self.answer = answer()
        # The headers of an answer that is a dict
        self.headers = dict(HEADERS)

    def handle(self, request: httpx.Request) -> httpx.Response:
        fields, files = {}, {}
        for name, filename, content_type, value in FIELD_RE.findall(request.content):
            if filename:
                files[name.decode()] = {'name': filename.decode(), 'type': content_type.decode(), 'content': value}
            else:
                fields[name.decode()] = value.decode()
        self.requests.append(
            {'url': str(request.url), 'key': request.headers.get('xi-api-key'), 'fields': fields, 'files': files}
        )
        reply = self.answer.pop(0) if isinstance(self.answer, list) else self.answer
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply, headers=self.headers)

    def client(self, api_key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers={'xi-api-key': api_key}, transport=httpx.MockTransport(self.handle))


def error(status: int, code: str, message: str = 'nope', headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={'detail': {'type': 'error', 'code': code, 'message': message}}, headers=headers)
