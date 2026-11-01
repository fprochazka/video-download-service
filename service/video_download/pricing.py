"""The price of a transcription: the amount the provider reported, or an estimate from its usage and a table of list
prices.

ElevenLabs reports the amount of money of each request in its response headers, see elevenlabs.provider_report; that
amount is the cost, and the estimate stays next to it for comparison. Gemini's response carries token counts only, so
its price is computed here and is an estimate: it uses the standard paid tier and does not know about free-tier quota,
included hours of a plan, discounts or price changes after the table was written. TRANSCRIPTION_PRICES replaces or adds
the prices of a model without a code change, see README; the app checks it at startup and does not start with an
invalid one.
"""

import json
import math
from dataclasses import dataclass, fields
from datetime import date
from functools import lru_cache

from video_download import config

SOURCE = 'https://ai.google.dev/gemini-api/docs/pricing'
# "Last updated 2026-10-30 UTC" on the pricing page, read on 2026-10-31
AS_OF = '2026-10-30'

# Gemini 3.x counts 25 tokens per second of audio; only used for transcriptions stored without the per-modality split
AUDIO_TOKENS_PER_SECOND = 25

PRICES_ENV = 'TRANSCRIPTION_PRICES'


ELEVENLABS_SOURCE = 'https://elevenlabs.io/pricing/api'
# The page shows no date of its own, read on 2026-11-01
ELEVENLABS_AS_OF = '2026-11-01'


@dataclass(frozen=True)
class Price:
    """USD per million tokens of one model, standard paid tier, or per hour of audio for a speech-to-text model"""

    # Prompt tokens of text, images and video, and of audio unless input_audio is set
    input: float | None = None
    # Output tokens, thinking tokens included: the pricing page says "Output price (including thinking tokens)"
    output: float | None = None
    input_audio: float | None = None
    # A request with more prompt tokens than this pays the long-context prices for all of its tokens
    long_context_tokens: int | None = None
    input_long: float | None = None
    output_long: float | None = None
    # ISO date the price applies from, for announced price changes; None applies from the start
    valid_from: str | None = None
    # When the price was read, and where from
    as_of: str | None = AS_OF
    source: str = SOURCE
    # USD per hour of audio, prorated per second; the token prices are then unused
    per_hour: float | None = None


def flash_2027(**overrides) -> list[Price]:
    """3.6 and 3.8 Flash double their price on 2027-01-01"""
    return [Price(0.75, 3.75, **overrides), Price(1.50, 7.50, valid_from='2027-01-01', **overrides)]


# gemini-3.5-flash is not on the Developer API pricing page, its transcriptions show tokens only
DEFAULT_PRICES: dict[str, list[Price]] = {
    'gemini-3.5-flash-lite': [Price(0.30, 2.50)],
    'gemini-3.1-flash-lite': [Price(0.25, 1.50, input_audio=0.50)],
    'gemini-3.6-flash': flash_2027(),
    'gemini-3.8-flash': flash_2027(),
    'gemini-3.1-pro-preview': [
        Price(2.00, 12.00, long_context_tokens=200_000, input_long=4.00, output_long=18.00),
    ],
    # The page prices its audio input and text output; the one text token of its prompt is priced as audio
    'gemini-3.5-transcribe': [Price(2.00, 12.00)],
    # The same price on every plan, keyterms and transcript editing cost extra and the service uses neither
    'scribe_v2': [Price(per_hour=0.22, as_of=ELEVENLABS_AS_OF, source=ELEVENLABS_SOURCE)],
}


FLOAT_FIELDS = ('input', 'output', 'input_audio', 'input_long', 'output_long', 'per_hour')


def price_fields(entry: dict) -> dict:
    """The known keys of one price object of the env, numbers checked; unknown keys are ignored"""
    known = {f.name for f in fields(Price)} - {'source'}
    values = {key: value for key, value in entry.items() if key in known}
    for key in FLOAT_FIELDS:
        if values.get(key) is not None:
            values[key] = float(values[key])
    if values.get('long_context_tokens') is not None:
        values['long_context_tokens'] = int(values['long_context_tokens'])
    if values.get('valid_from') is not None:
        date.fromisoformat(values['valid_from'])
    if values.get('per_hour') is None and (values.get('input') is None or values.get('output') is None):
        raise ValueError('a price needs input and output, or per_hour')
    return values


@lru_cache(maxsize=4)
def parse_overrides(raw: str) -> dict[str, list[Price]]:
    """{"model": {"input": 0.3, "output": 2.5, ...}} or {"model": {"per_hour": 0.22}}, or a list of such objects per
    model, for dated prices"""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('expected a JSON object of model names')
        overrides = {}
        for model, entries in data.items():
            entries = entries if isinstance(entries, list) else [entries]
            overrides[model] = [
                Price(**({'as_of': None} | price_fields(entry) | {'source': PRICES_ENV})) for entry in entries
            ]
        return overrides
    except (ValueError, TypeError, AttributeError) as e:
        raise config.ConfigError(f'{PRICES_ENV} is not a valid JSON object of prices, see README: {e}') from None


def prices() -> dict[str, list[Price]]:
    """The built-in prices with those of TRANSCRIPTION_PRICES; raises ConfigError for an invalid one"""
    raw = config.env_value(PRICES_ENV)
    return DEFAULT_PRICES | (parse_overrides(raw) if raw else {})


def price_of(model: str | None, on: date) -> Price | None:
    """The price of the model on that day, None for a model without a known price"""
    candidates = [
        p for p in prices().get(model or '', []) if p.valid_from is None or date.fromisoformat(p.valid_from) <= on
    ]
    return max(candidates, key=lambda p: p.valid_from or '', default=None)


def request_cost(price: Price, usage: dict) -> float:
    """USD of one request's usage: prompt_tokens (prompt_audio_tokens of them audio), output_tokens, thinking_tokens,
    or audio_seconds for a price per hour"""
    if price.per_hour is not None:
        return math.ceil(usage.get('audio_seconds') or 0) * price.per_hour / 3600
    prompt = usage.get('prompt_tokens') or 0
    audio = min(prompt, usage.get('prompt_audio_tokens') or 0)
    output = (usage.get('output_tokens') or 0) + (usage.get('thinking_tokens') or 0)
    long_context = price.long_context_tokens is not None and prompt > price.long_context_tokens
    input_price = price.input_long if long_context and price.input_long is not None else price.input
    output_price = price.output_long if long_context and price.output_long is not None else price.output
    audio_price = input_price if price.input_audio is None or long_context else price.input_audio
    return ((prompt - audio) * input_price + audio * audio_price + output * output_price) / 1_000_000


def estimate(model: str | None, requests: list[dict], on: date) -> dict | None:
    """The cost entry of a transcription made of these requests' usages, None for a model without a known price"""
    price = price_of(model, on)
    if price is None:
        return None
    usd = round(sum(request_cost(price, usage) for usage in requests), 6)
    if price.per_hour is not None:
        prices = {'model': model, 'per_hour': price.per_hour, 'as_of': price.as_of, 'source': price.source}
        return {'usd': usd, 'estimated': True, 'prices': prices}
    return {
        'usd': usd,
        'estimated': True,
        'prices': {
            'model': model,
            'input_text': price.input,
            'input_audio': price.input_audio if price.input_audio is not None else price.input,
            'output': price.output,
            'as_of': price.as_of,
            'source': price.source,
        },
    }


def estimate_stored(model: str | None, usage: dict | None, duration: float | None, on: date) -> dict | None:
    """The cost of a transcription stored before its cost was: its usage has no audio count, so the audio is assumed
    to be duration × 25 tokens of the prompt, or all of it when the duration is unknown"""
    if not usage:
        return None
    if usage.get('prompt_audio_tokens') is None:
        prompt = usage.get('prompt_tokens') or 0
        audio = prompt if not duration else min(prompt, math.ceil(duration * AUDIO_TOKENS_PER_SECOND))
        usage = usage | {'prompt_audio_tokens': audio}
    return estimate(model, [usage], on)


def with_reported(estimated: dict | None, reported: dict | None) -> dict | None:
    """The cost entry of a transcription whose provider reported {amount, currency}: the reported amount when it is in
    USD, with the estimate as estimate_usd; the estimate alone when there is no amount in USD"""
    if not reported or reported.get('currency') != 'usd':
        return estimated
    cost = {'usd': reported['amount'], 'estimated': False, 'source': 'provider'}
    if estimated:
        cost |= {'estimate_usd': estimated['usd'], 'prices': estimated['prices']}
    return cost
