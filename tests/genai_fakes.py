"""A stand-in for the google-genai client: records what the service sent, answers what the test set"""

from pathlib import Path

from google.genai import types

API_KEY = 'test-secret-key-0123456789'

DEFAULT_LINES = [
    (1, 'Speaker 1', 'Hello and welcome.'),
    (4, 'Speaker 1', 'Today we talk about engines.'),
    (10, 'Speaker 2', 'Thanks for having me.'),
]


def transcript_lines(lines=None) -> str:
    """The answer the service asks Gemini for: [HH:MM:SS] Speaker: text lines, from (seconds, speaker, text)"""
    return '\n'.join(
        f'[{start // 3600:02d}:{start % 3600 // 60:02d}:{start % 60:02d}] {speaker}: {text}'
        for start, speaker, text in (DEFAULT_LINES if lines is None else lines)
    )


def response(text: str | None = None, finish_reason='STOP', usage=(1000, 200, 0), block_reason=None, audio_tokens=None):
    """audio_tokens of the prompt tokens are audio, the rest text; by default three quarters"""
    prompt_tokens, output_tokens, thinking_tokens = usage
    audio = prompt_tokens * 3 // 4 if audio_tokens is None else audio_tokens
    data = {
        'usage_metadata': {
            'prompt_token_count': prompt_tokens,
            'prompt_tokens_details': [
                {'modality': 'TEXT', 'token_count': prompt_tokens - audio},
                {'modality': 'AUDIO', 'token_count': audio},
            ],
            'candidates_token_count': output_tokens,
            'thoughts_token_count': thinking_tokens,
        },
    }
    if block_reason:
        data['prompt_feedback'] = {'block_reason': block_reason}
    else:
        data['candidates'] = [
            {
                'content': {'role': 'model', 'parts': [{'text': transcript_lines() if text is None else text}]},
                'finish_reason': finish_reason,
            }
        ]
    return types.GenerateContentResponse.model_validate(data)


class FakeGenai:
    """genai.Client(...).aio: files.upload/get/delete and models.generate_content"""

    def __init__(self):
        self.api_keys = []
        self.uploads = []
        self.deleted = []
        self.requests = []
        # What generate_content returns, or raises when it is an exception; a function of the request's contents
        # answers each request on its own
        self.answer = response()
        self.upload_error = None
        self.processing_polls = 1

    # genai.Client
    def client(self, api_key):
        self.api_keys.append(api_key)
        return self

    @property
    def aio(self):
        return self

    @property
    def files(self):
        return self

    @property
    def models(self):
        return self

    async def upload(self, *, file, config):
        if self.upload_error:
            raise self.upload_error
        path = Path(file)
        self.uploads.append({'name': path.name, 'content': path.read_bytes(), 'mime_type': config.mime_type})
        return types.File(name='files/fake1', uri='https://fake/files/fake1', state='PROCESSING', mime_type='audio/ogg')

    async def get(self, *, name):
        self.processing_polls -= 1
        state = 'PROCESSING' if self.processing_polls > 0 else 'ACTIVE'
        return types.File(name=name, uri='https://fake/files/fake1', state=state, mime_type='audio/ogg')

    async def delete(self, *, name):
        self.deleted.append(name)

    async def generate_content(self, *, model, contents, config):
        self.requests.append({'model': model, 'contents': contents, 'config': config})
        answer = self.answer(contents) if callable(self.answer) else self.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer
