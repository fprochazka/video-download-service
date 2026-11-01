"""Run the service's Gemini transcription on a downloaded job and save every answer, or replay saved answers.

    python benchmarks/pipeline.py record <downloads_dir> <job_id> <media_name> <calls_dir> --ledger ledger.md --cap 1.00 [--spend]
    python benchmarks/pipeline.py replay <downloads_dir> <job_id> <media_name> <calls_dir> [<calls_dir> ...]

record calls Gemini and costs money. Without --spend it only prints the estimated cost. It needs TRANSCRIPTION_PROVIDER
and GEMINI_API_KEY like the service. Every call is appended to the ledger, and no call starts once the ledger's total
reaches --cap.

replay runs the current pipeline with the saved answers in place of Gemini, at no cost: a change to the checks, the
repairs or the stitching can be measured on answers that were already paid for. An answer is picked by the chunk
index and start second in the audio file name; a chunk the changed code cuts differently has no saved answer and fails.

Both write the transcript into the job directory, so run them on a copy of the job.
"""

import argparse
import asyncio
import contextvars
import json
import math
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from google.genai import types

import metrics  # noqa: F401  (puts the service on the import path)
from video_download import chunking, config, gemini, pricing, transcription
from video_download.gemini import GeminiProvider, ProviderError

# The chunk audio is named <stem>-<chunk index>-<start second>.ogg
CHUNK_AUDIO_RE = re.compile(r'-(\d{3})-(\d+)\.\w+$')
# Rough sizes of one chunk request, from the recorded runs: the prompt carries the title, the description and the
# subtitles of the chunk, the answer about 10 tokens per second of speech; retries add up to a third
PROMPT_TEXT_TOKENS_PER_CHUNK = 3000
OUTPUT_TOKENS_PER_SECOND = 10
RETRY_FACTOR = 1.3


def chunk_key(audio_name: str) -> tuple[int, int] | None:
    match = CHUNK_AUDIO_RE.search(audio_name)
    return (int(match[1]), int(match[2])) if match else None


def job_duration(job_dir: Path) -> float:
    info = transcription.read_info(job_dir)
    return float(info.get('duration') or json.loads((job_dir / 'metadata.json').read_text())['duration'])


def estimate_usd(model: str, duration: float) -> float | None:
    price = pricing.price_of(model, datetime.now(UTC).date())
    if price is None:
        return None
    chunks = max(1, math.ceil(duration / chunking.DEFAULT_TARGET_SECONDS))
    audio = math.ceil(duration * pricing.AUDIO_TOKENS_PER_SECOND)
    usage = {
        'prompt_tokens': audio + chunks * PROMPT_TEXT_TOKENS_PER_CHUNK,
        'prompt_audio_tokens': audio,
        'output_tokens': math.ceil(duration * OUTPUT_TOKENS_PER_SECOND),
    }
    return pricing.request_cost(price, usage) * RETRY_FACTOR


class Ledger:
    """A Markdown table of every paid call, with the running total; the cap counts the whole file"""

    HEADER = '| # | call | prompt tokens | audio tokens | output tokens | thinking tokens | usd | total usd |\n'

    def __init__(self, path: Path, cap: float):
        self.path, self.cap = path, cap
        if not path.exists():
            path.write_text(self.HEADER + '|---|---|---|---|---|---|---|---|\n')

    def rows(self) -> list[list[str]]:
        lines = self.path.read_text().splitlines()
        return [[c.strip() for c in line.strip('|').split('|')] for line in lines if re.match(r'^\| \d+ ', line)]

    def total(self) -> float:
        rows = self.rows()
        return float(rows[-1][-1]) if rows else 0.0

    def record(self, call: str, usage: gemini.Usage, usd: float) -> float:
        total = self.total() + usd
        with self.path.open('a') as f:
            f.write(
                f'| {len(self.rows()) + 1} | {call} | {usage.prompt_tokens} | {usage.prompt_audio_tokens} '
                f'| {usage.output_tokens} | {usage.thinking_tokens} | {usd:.5f} | {total:.5f} |\n'
            )
        return total


def record(args):
    settings = config.transcription_settings()
    if settings.provider != 'gemini':
        raise SystemExit('record needs TRANSCRIPTION_PROVIDER=gemini and GEMINI_API_KEY')
    duration = job_duration(args.downloads / args.job)
    usd = estimate_usd(settings.model, duration)
    estimate = f'${usd:.2f}' if usd is not None else 'unknown (no price for the model)'
    print(f'{settings.model}, {duration / 60:.1f} min of audio: estimated cost {estimate}', flush=True)
    if not args.spend:
        print('Not calling Gemini without --spend.')
        return
    ledger = Ledger(args.ledger, args.cap)
    price = pricing.price_of(settings.model, datetime.now(UTC).date())
    args.calls.mkdir(parents=True, exist_ok=True)
    original_transcribe = GeminiProvider.transcribe
    original_response = GeminiProvider.provider_response
    current = contextvars.ContextVar('current')
    count = [0]

    async def transcribe(self, request):
        if ledger.total() >= ledger.cap:
            raise ProviderError('provider_error', f'the ledger reached its cap of ${ledger.cap:.2f}, no more calls')
        count[0] += 1
        name = f'{count[0]:03d}'
        shutil.copy(request.audio, args.calls / f'{name}.ogg')
        (args.calls / f'{name}.prompt.txt').write_text(request.prompt)
        request_fields = {'display_name': request.display_name, 'max_output_tokens': request.max_output_tokens}
        (args.calls / f'{name}.request.json').write_text(json.dumps(request_fields | {'audio': request.audio.name}))
        current.set(name)
        started = time.monotonic()
        result = await original_transcribe(self, request)
        meta = {'latency': round(time.monotonic() - started, 1), 'finish_reason': result.finish_reason}
        (args.calls / f'{name}.meta.json').write_text(json.dumps(meta | {'usage': result.usage.__dict__}))
        return result

    def provider_response(response):
        name = current.get()
        (args.calls / f'{name}.raw.json').write_text(response.model_dump_json(indent=1, exclude_none=True))
        result = original_response(response)
        usd = pricing.request_cost(price, result.usage.__dict__) if price else 0.0
        print(f'call {name}: ${usd:.4f}, ledger total ${ledger.record(name, result.usage, usd):.4f}', flush=True)
        return result

    GeminiProvider.transcribe = transcribe
    GeminiProvider.provider_response = staticmethod(provider_response)
    run(args)


def replay(args):
    answers = defaultdict(list)
    for calls in args.calls:
        for request in sorted(calls.glob('*.request.json')):
            raw = request.with_name(request.name.replace('.request.json', '.raw.json'))
            key = chunk_key(json.loads(request.read_text())['audio'])
            if key and raw.exists():
                answers[key].append(raw)
    used, missing = [], []

    class Replay:
        def __init__(self, model: str):
            self.model = model

        async def transcribe(self, request):
            key = chunk_key(request.audio.name)
            if not answers.get(key):
                missing.append(key)
                raise ProviderError('provider_error', f'no saved answer for chunk {key}')
            raw = answers[key].pop(0)
            used.append(str(raw))
            response = types.GenerateContentResponse.model_validate_json(raw.read_text())
            return GeminiProvider.provider_response(response)

    # The settings must name a Gemini model; the key is never used
    os.environ.setdefault('TRANSCRIPTION_PROVIDER', 'gemini')
    os.environ.setdefault('GEMINI_API_KEY', 'unused-by-replay')
    transcription.make_provider = lambda settings: Replay(settings.model)
    run(args)
    print(json.dumps({'answers_used': len(used), 'chunks_without_answer': missing}))


def run(args):
    config.DOWNLOADS_DIR = args.downloads
    started = time.monotonic()
    asyncio.run(transcription.transcribe_media_task(args.job, args.media))
    state = json.loads((args.downloads / args.job / 'metadata.json').read_text())['transcriptions'][args.media]
    summary = {k: state.get(k) for k in ('status', 'error_code', 'error', 'warnings', 'usage', 'cost')}
    print(json.dumps(summary | {'wall_seconds': round(time.monotonic() - started, 1)}, indent=1))
    for chunk in state.get('chunks') or []:
        attempts = [
            {
                'problems': [p['code'] for p in a.get('problems') or []],
                'error': a.get('error'),
                'repaired': a.get('repaired_timestamps'),
            }
            for a in chunk['attempts']
        ]
        fields = {k: chunk.get(k) for k in ('index', 'start', 'end', 'split_at')}
        print(json.dumps(fields | {'attempts': attempts}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('record', 'replay'):
        command = commands.add_parser(name)
        command.add_argument('downloads', type=Path, help='the downloads directory that holds the job')
        command.add_argument('job', help='the job id, a directory in downloads')
        command.add_argument('media', help='the media file name in the job directory')
    rec = commands.choices['record']
    rec.add_argument('calls', type=Path, help='directory for the saved requests and answers')
    rec.add_argument('--ledger', type=Path, required=True, help='Markdown spend ledger, created when missing')
    rec.add_argument('--cap', type=float, required=True, help='USD the ledger total may reach')
    rec.add_argument('--spend', action='store_true', help='call Gemini; without it only the estimate is printed')
    commands.choices['replay'].add_argument('calls', type=Path, nargs='+', help='directories written by record')
    args = parser.parse_args()
    record(args) if args.command == 'record' else replay(args)


if __name__ == '__main__':
    main()
