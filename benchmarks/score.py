"""Score a transcript against YouTube's speech recognition captions of the same recording; prints JSON.

    python benchmarks/score.py <captions.vtt> <transcript> [--speech speech.json] [--czech]

The transcript is a .transcript.json or .transcript.md of the service, a raw ElevenLabs Scribe answer, or a JSON list
of {start, speaker, text} segments. See docs/benchmark-methodology.md for what each number means.
"""

import argparse
import json
from pathlib import Path

import metrics


def score(ref: list[metrics.RefWord], tr: metrics.Transcript, speech=None, czech: bool = False) -> dict:
    alignment = metrics.align_words(ref, tr.words)
    ref_tokens = [r.word for r in ref]
    hyp_tokens = [h.word for h in tr.words]
    out = {
        'caption_words': len(ref),
        'transcript_words': len(tr.words),
        'segments': len(tr.segments),
        **metrics.coverage(ref_tokens, hyp_tokens),
        'normalized': metrics.coverage(metrics.normalize(ref_tokens), metrics.normalize(hyp_tokens)),
        'drift': metrics.anchor_drift(ref, tr.words, alignment),
        'longest_unmatched_captions': metrics.longest_unmatched_run(ref, alignment),
        'lost_in_runs_of_8': metrics.lost_in_long_runs(alignment),
        'repeated_lines': metrics.repeated_lines(tr.segments),
        'speaker_changes_1s': metrics.speaker_changes(ref, tr.words, alignment),
        'turns': metrics.turn_purity(ref, tr.words, alignment),
        'numbers': {'captions': metrics.number_style(ref_tokens), 'transcript': metrics.number_style(hyp_tokens)},
    }
    if tr.word_times:
        out['word_timing'] = metrics.word_timing(ref, tr.words, alignment)
    if tr.segments:
        last = max(float(s.get('end', s['start'])) for s in tr.segments)
        out['last_segment_end'] = last
        if tr.duration:
            out['end_gap'] = round(tr.duration - last, 1)
    if speech is not None:
        out['blind_speech_seconds'] = metrics.blind_speech(tr, speech)
    if tr.chunks:
        out['chunks'] = len(tr.chunks)
    if czech:
        out['diacritics_share'] = {
            'captions': metrics.diacritics_share(ref_tokens),
            'transcript': metrics.diacritics_share(hyp_tokens),
        }
        out['colloquial'] = {
            'captions': metrics.colloquial_counts(ref_tokens),
            'transcript': metrics.colloquial_counts(hyp_tokens),
        }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('captions', type=Path, help='the .vtt captions, ideally the -orig speech recognition track')
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--speech', type=Path, help='speech regions from python -m video_download.vad, for blind gaps')
    parser.add_argument('--czech', action='store_true', help='also count diacritics and colloquial Czech forms')
    parser.add_argument(
        '--drop-annotations', action='store_true', help='leave [music] and the like out of the captions'
    )
    args = parser.parse_args()
    ref = metrics.reference_words(args.captions.read_text(), args.drop_annotations)
    speech = metrics.load_speech(args.speech) if args.speech else None
    result = score(ref, metrics.load_transcript(args.transcript), speech, args.czech)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
