"""Transcript quality measures against YouTube's speech recognition captions, see docs/benchmark-methodology.md.

The captions are the reference, the transcript under test is the hypothesis. Both become lists of normalized words with
a start time, and difflib aligns them; every measure below reads that alignment. Words are normalized with the
service's own chunking.words(), so a benchmark and the pipeline's coverage check count the same thing.
"""

import bisect
import difflib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'service'))

from video_download import transcription  # noqa: E402
from video_download.chunking import largest_blind_speech, words  # noqa: E402
from video_download.subtitles import CaptionWord, vtt_words  # noqa: E402


@dataclass(frozen=True)
class RefWord:
    time: float
    word: str
    # The caption word starts a new speaker's turn (>> in YouTube's captions)
    speaker_change: bool = False
    # The word is part of a bracketed annotation such as [music] or [applause]
    annotation: bool = False


@dataclass(frozen=True)
class HypWord:
    time: float
    word: str
    speaker: str | None = None
    # The chunk of a chunked transcript, whose speaker labels restart; None for a whole-file transcript
    chunk: int | None = None


@dataclass
class Transcript:
    words: list[HypWord]
    # Service-shaped segments: start, speaker, text, and chunk for a chunked transcript
    segments: list[dict]
    duration: float | None = None
    # (start, end) of each chunk of a chunked transcript
    chunks: list[tuple[float, float]] = field(default_factory=list)
    # The words carry their own start time, not the time of their segment
    word_times: bool = False


# --- Loading


def reference_words(vtt: str, drop_annotations: bool = False) -> list[RefWord]:
    """The normalized words of a WebVTT file, each with the time of its caption word.

    YouTube's speech recognition captions time every word; other subtitles get their words spread over the cue.
    """
    out = []
    in_annotation = False
    for caption in vtt_words(vtt):
        # An annotation can span several caption words: [gentle music]
        text = caption.text.rstrip('.,')
        annotation = in_annotation or text.startswith('[')
        if annotation:
            in_annotation = not text.endswith(']')
        if annotation and drop_annotations:
            continue
        for i, word in enumerate(words(caption.text)):
            out.append(RefWord(caption.time, word, caption.speaker_change and i == 0, annotation))
    return out


def scribe_segments(scribe_words: list[dict]) -> list[dict]:
    """ElevenLabs Scribe words grouped into sentences of one speaker, like the service's segments"""
    segments: list[dict] = []
    current = None
    for w in scribe_words:
        if w['type'] == 'spacing':
            if current:
                current['text'] += w['text']
            continue
        speaker = w.get('speaker_id')
        if current is None or speaker != current['speaker']:
            if current:
                segments.append(current)
            current = {'start': w['start'], 'speaker': speaker, 'text': ''}
        current['text'] += w['text']
        if w['type'] == 'word' and w['text'].rstrip('"”’)').endswith(('.', '?', '!')):
            segments.append(current)
            current = None
    if current:
        segments.append(current)
    for segment in segments:
        segment['text'] = ' '.join(segment['text'].split())
    return [s for s in segments if s['text']]


def transcript_from_segments(segments: list[dict], duration=None, chunks=()) -> Transcript:
    hyp = [HypWord(float(s['start']), w, s.get('speaker'), s.get('chunk')) for s in segments for w in words(s['text'])]
    return Transcript(hyp, segments, duration, list(chunks))


def load_transcript(path: Path) -> Transcript:
    """A .transcript.json or .transcript.md of the service, a raw ElevenLabs Scribe answer, or a JSON list of
    segments ({start, speaker, text})"""
    if path.suffix == '.md':
        # The Markdown has one timestamp per paragraph, so its timing measures are coarse
        parsed = transcription.parse_transcript(path.read_text())
        return transcript_from_segments(parsed.segments)
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return transcript_from_segments(data)
    if 'words' in data and 'segments' not in data:
        hyp = [
            HypWord(float(w['start']), token, w.get('speaker_id'))
            for w in data['words']
            if w['type'] == 'word'
            for token in words(w['text'])
        ]
        segments = scribe_segments(data['words'])
        return Transcript(hyp, segments, data.get('audio_duration_secs'), word_times=True)
    chunks = [(float(c['start']), float(c['end'])) for c in data.get('chunks') or []]
    return transcript_from_segments(data['segments'], data.get('duration'), chunks)


def load_speech(path: Path) -> list[tuple[float, float]]:
    """The speech regions written by python -m video_download.vad"""
    return [(float(s), float(e)) for s, e in json.loads(path.read_text())['regions']]


# --- Normalization

# Spoken forms one provider writes and the captions spell out, or the other way round
SPOKEN_FORMS = {
    'gonna': 'going to',
    'gotta': 'got to',
    'wanna': 'want to',
    'kinda': 'kind of',
    'cause': 'because',
    'cuz': 'because',
    'todle': 'tohle',
    'anebo': 'a nebo',
}
NUMBER_WORDS = frozenset(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen '
    'eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million billion percent '
    'nula jedna jeden jedno dva dvě tři čtyři pět šest sedm osm devět deset jedenáct dvanáct třináct čtrnáct patnáct '
    'šestnáct sedmnáct osmnáct devatenáct dvacet třicet čtyřicet padesát šedesát sedmdesát osmdesát devadesát sto stě '
    'set tisíc tisíce milion milionů miliony miliard miliardy procent pěti dvaceti třiceti stovky'.split()
)


def normalize(tokens: list[str]) -> list[str]:
    """Spoken forms spelled out, numbers left out: "gonna" against "going to" or "66" against "sixty six" is a spelling
    difference, not a missed word"""
    out = []
    for token in tokens:
        for part in SPOKEN_FORMS.get(token, token).split():
            if not part.isdigit() and part not in NUMBER_WORDS:
                out.append(part)
    return out


# --- Alignment


@dataclass
class Alignment:
    blocks: list  # difflib Match(a, b, size), a indexes the reference, b the hypothesis
    opcodes: list
    ref_to_hyp: dict[int, int]

    @property
    def matched(self) -> int:
        return len(self.ref_to_hyp)


def align(ref: list[str], hyp: list[str]) -> Alignment:
    # autojunk would drop the most frequent words ("the", "a") from the matching on any long recording
    matcher = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    blocks = matcher.get_matching_blocks()
    mapping = {b.a + k: b.b + k for b in blocks for k in range(b.size)}
    return Alignment(blocks, matcher.get_opcodes(), mapping)


def align_words(ref: list[RefWord], hyp: list[HypWord]) -> Alignment:
    return align([r.word for r in ref], [h.word for h in hyp])


# --- Measures


def coverage(ref: list[str], hyp: list[str]) -> dict:
    """recall: the share of caption words the transcript has, in order; insertions: the share of transcript words
    that are not in the captions"""
    matched = align(ref, hyp).matched
    return {
        'recall': round(matched / max(1, len(ref)), 3),
        'insertions': round((len(hyp) - matched) / max(1, len(hyp)), 3),
    }


def anchor_drift(ref, hyp, alignment: Alignment, min_run: int = 4, spacing: float = 20.0) -> dict:
    """Timestamp offset (transcript minus captions) at anchors: a word inside a run of at least min_run matched words,
    one anchor at most every spacing seconds. A run of several words in the same order is the same passage, a lone
    common word ("the") can match anywhere."""
    offsets = []
    last = float('-inf')
    for b in alignment.blocks:
        if b.size < min_run:
            continue
        for k in range(0, b.size, min_run):
            t = ref[b.a + k].time
            if t - last >= spacing:
                offsets.append((round(t), round(hyp[b.b + k].time - t, 2)))
                last = t
    if not offsets:
        return {'anchors': 0}
    absolute = sorted(abs(o) for _, o in offsets)
    return {
        'anchors': len(offsets),
        'median': round(statistics.median(absolute), 2),
        'p95': round(absolute[int(0.95 * len(absolute))], 2),
        'max': round(absolute[-1], 1),
        'worst': max(offsets, key=lambda x: abs(x[1])),
    }


def word_timing(ref, hyp, alignment: Alignment) -> dict:
    """Offset of every aligned word, for a transcript with a time per word"""
    offsets = sorted(abs(hyp[j].time - ref[i].time) for i, j in alignment.ref_to_hyp.items())
    if not offsets:
        return {}
    return {
        'median': round(statistics.median(offsets), 3),
        'p90': round(offsets[int(0.9 * len(offsets))], 2),
        'p99': round(offsets[int(0.99 * len(offsets))], 2),
        'within_0.3s': round(sum(x <= 0.3 for x in offsets) / len(offsets), 3),
        'within_1s': round(sum(x <= 1 for x in offsets) / len(offsets), 3),
    }


def longest_unmatched_run(ref, alignment: Alignment, min_words: int = 5) -> dict:
    """The longest stretch of captions, in seconds, with at least min_words caption words and none of them in the
    transcript: a passage the transcript skipped"""
    matched = sorted(alignment.ref_to_hyp)
    best = {'seconds': 0.0, 'at': None, 'caption_words': 0}
    for p, q in zip(matched, matched[1:], strict=False):
        missing = q - p - 1
        seconds = ref[q].time - ref[p].time
        if missing >= min_words and seconds > best['seconds']:
            best = {'seconds': round(seconds, 1), 'at': round(ref[p].time), 'caption_words': missing}
    return best


def lost_in_long_runs(alignment: Alignment, min_run: int = 8) -> int:
    """Caption words deleted or replaced in runs of at least min_run: lost passages, not spelling differences"""
    return sum(i2 - i1 for op, i1, i2, _, _ in alignment.opcodes if op in ('delete', 'replace') and i2 - i1 >= min_run)


def blind_speech(transcript: Transcript, speech: list[tuple[float, float]]) -> float:
    """The most detected speech between two consecutive segment starts: the pipeline's own check of a skipped passage"""
    duration = transcript.duration or max((s for _, s in speech), default=0.0)
    return round(largest_blind_speech([float(s['start']) for s in transcript.segments], speech, 0, duration), 1)


def seam_loss(ref: list[RefWord], segments: list[dict], seam: float, window: float = 5.0) -> dict:
    """Caption words within window seconds of a chunk seam that the transcript lacks, and three-word phrases that
    appear on both sides of it (a passage transcribed twice)"""
    near_ref = [r for r in ref if seam - 20 <= r.time < seam + 20]
    near_hyp = [w for s in segments if seam - 30 <= s['start'] < seam + 30 for w in words(s['text'])]
    matched = align([r.word for r in near_ref], near_hyp).ref_to_hyp
    near = [i for i, r in enumerate(near_ref) if seam - window <= r.time < seam + window]
    lost = [near_ref[i].word for i in near if i not in matched]

    def trigrams(tokens):
        return {' '.join(tokens[i : i + 3]) for i in range(len(tokens) - 2)}

    before = [w for s in segments if seam - 15 <= s['start'] < seam for w in words(s['text'])]
    after = [w for s in segments if seam <= s['start'] < seam + 15 for w in words(s['text'])]
    return {
        'at': round(seam, 1),
        'caption_words': len(near),
        'lost': len(lost),
        'lost_words': ' '.join(lost),
        'duplicated_trigrams': len(trigrams(before) & trigrams(after)),
    }


def speaker_changes(ref, hyp, alignment: Alignment, tolerance: float = 1.0) -> dict:
    """Precision and recall of the transcript's speaker changes against the captions' >> markers.

    A transcript change gets the caption time of the next aligned word, at most 5 words on, so its timestamp accuracy
    does not count; each marker matches at most one change within the tolerance."""
    hyp_to_ref = {j: i for i, j in alignment.ref_to_hyp.items()}
    aligned = sorted(hyp_to_ref)

    def caption_time(j):
        k = bisect.bisect_left(aligned, j)
        if k < len(aligned) and aligned[k] - j <= 5:
            return ref[hyp_to_ref[aligned[k]]].time
        return None

    changes = [j for j in range(1, len(hyp)) if hyp[j].speaker != hyp[j - 1].speaker]
    times = sorted(t for t in (caption_time(j) for j in changes) if t is not None)
    markers = [r.time for r in ref if r.speaker_change]
    return match_changes(times, markers, tolerance)


def match_changes(times: list[float], markers: list[float], tolerance: float) -> dict:
    used: set[int] = set()
    hits = 0
    for t in sorted(times):
        k = bisect.bisect_left(markers, t - tolerance)
        while k < len(markers) and markers[k] <= t + tolerance and k in used:
            k += 1
        if k < len(markers) and markers[k] <= t + tolerance:
            used.add(k)
            hits += 1
    return {
        'changes': len(times),
        'markers': len(markers),
        'precision': round(hits / max(1, len(times)), 3),
        'recall': round(hits / max(1, len(markers)), 3),
    }


def diarization_changes(turns: list[tuple[float, float, str]], markers: list[float], tolerance: float = 1.0) -> dict:
    """Speaker changes of a diarization, (start, end, label) turns without words, against the captions' >> markers.

    A change spans from the end of one label's turn to the start of the next label's turn, and matches a marker that
    lies within the tolerance of that span."""
    spans = []
    for (_, end, label), (start, _, following) in zip(turns, turns[1:], strict=False):
        if label != following:
            spans.append((min(end, start), max(end, start)))

    def near(t, lo, hi):
        return lo - tolerance <= t <= hi + tolerance

    markers_hit = sum(any(near(m, lo, hi) for lo, hi in spans) for m in markers)
    changes_hit = sum(any(near(m, lo, hi) for m in markers) for lo, hi in spans)
    return {
        'changes': len(spans),
        'markers': len(markers),
        'markers_with_change': round(markers_hit / max(1, len(markers)), 3),
        'changes_with_marker': round(changes_hit / max(1, len(spans)), 3),
    }


def turn_purity(ref, hyp, alignment: Alignment, min_words: int = 15) -> dict:
    """The captions split into turns at >> markers; purity is the share of a turn's aligned words that carry the
    turn's most frequent speaker label, weighted by words. Labels of a chunked transcript count per chunk."""
    turns: list[list[int]] = [[]]
    for i, r in enumerate(ref):
        if r.speaker_change and turns[-1]:
            turns.append([])
        turns[-1].append(i)
    rows = []
    for turn in turns:
        labels = Counter((hyp[j].speaker, hyp[j].chunk) for i in turn if (j := alignment.ref_to_hyp.get(i)) is not None)
        total = sum(labels.values())
        if total >= min_words:
            label, count = labels.most_common(1)[0]
            rows.append((label, count / total, total))
    if not rows:
        return {'turns': 0}
    # Consecutive turns should alternate labels; across a chunk seam the labels are not comparable
    pairs = [a[0][0] != b[0][0] for a, b in zip(rows, rows[1:], strict=False) if a[0][1] == b[0][1]]
    return {
        'turns': len(rows),
        'purity': round(sum(p * n for _, p, n in rows) / sum(n for *_, n in rows), 3),
        'turns_below_0.8': sum(p < 0.8 for _, p, _ in rows),
        'alternating': round(sum(pairs) / max(1, len(pairs)), 3),
    }


def label_agreement(a: Transcript, b: Transcript) -> dict:
    """How often a's speaker label maps to b's label of the same word, after aligning a's words to b's.

    Each label of a maps to the label of b it most often coincides with: once for the whole file (global), and again
    for each chunk of a (per chunk). The gap between the two is what label restarts at chunk seams cost."""
    alignment = align([w.word for w in a.words], [w.word for w in b.words])
    per_chunk: dict = defaultdict(Counter)
    for i, j in alignment.ref_to_hyp.items():
        per_chunk[a.words[i].chunk][(a.words[i].speaker, b.words[j].speaker)] += 1
    whole: Counter = Counter()
    for counts in per_chunk.values():
        whole.update(counts)

    def agreeing(counts: Counter) -> int:
        best: dict = {}
        for (la, _), n in counts.items():
            best[la] = max(best.get(la, 0), n)
        return sum(best.values())

    total = sum(whole.values())
    return {
        'aligned_words': total,
        'agreement_global_mapping': round(agreeing(whole) / max(1, total), 3),
        'agreement_per_chunk_mapping': round(sum(agreeing(c) for c in per_chunk.values()) / max(1, total), 3),
    }


def repeated_lines(segments: list[dict]) -> int:
    return transcription.repeated_lines(s['text'] for s in segments)


CZECH_DIACRITICS_RE = re.compile('[áčďéěíňóřšťúůýž]')
CZECH_COLLOQUIAL = {
    '-ej endings (kterej, nějakej)': r'\w+ej$',
    'kterej': r'^kterej$',
    'todle/tadle': r'^t[oa]dle$',
    'bysme': r'^bysme$',
    'jo': r'^jo$',
}


def diacritics_share(tokens: list[str]) -> float:
    """The share of words with a Czech diacritic; a transcript that drops them reads as broken Czech"""
    return round(sum(bool(CZECH_DIACRITICS_RE.search(t)) for t in tokens) / max(1, len(tokens)), 3)


def colloquial_counts(tokens: list[str], patterns: dict[str, str] = CZECH_COLLOQUIAL) -> dict:
    return {name: sum(bool(re.search(p, t)) for t in tokens) for name, p in patterns.items()}


def number_style(tokens: list[str]) -> dict:
    return {'digit_tokens': sum(t.isdigit() for t in tokens), 'number_words': sum(t in NUMBER_WORDS for t in tokens)}


# --- Cut placement, without any transcript

SENTENCE_END_RE = re.compile(r'[.?!…]["\'”’)\]]*$')


def cut_quality(captions: list[CaptionWord], cut: float) -> dict:
    """What the captions show at a cut: whether the word before it ends a sentence, whether the word after it starts a
    speaker's turn, and the seconds from the cut to the next caption word"""
    times = [c.time for c in captions]
    i = bisect.bisect_right(times, cut) - 1
    before = captions[i] if i >= 0 else None
    after = captions[i + 1] if i + 1 < len(captions) else None
    sentence_end = bool(before and SENTENCE_END_RE.search(before.text))
    speaker = bool(after and after.speaker_change)
    return {
        'at': round(cut, 2),
        'before': before.text if before else None,
        'after': after.text if after else None,
        'sentence_end': sentence_end,
        'speaker_change': speaker,
        'boundary': sentence_end or speaker,
        'lead_seconds': round(after.time - cut, 2) if after else None,
    }
