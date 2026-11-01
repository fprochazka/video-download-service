"""Where the chunk cuts fall, judged from the captions, with no transcription call; prints JSON, one line per cut.

    python benchmarks/cuts.py <captions.vtt> --transcript <transcript.json>
    python benchmarks/cuts.py <captions.vtt> --plan <job_dir> --speech <speech.json> [--no-chapters] [--no-captions]

--transcript reads the cuts of a finished chunked transcription. --plan runs the service's current chunk planner on a
downloaded job (its info.json gives the duration and chapters), so a change to the planner can be judged before any
money is spent. --no-captions plans as the service does without subtitles: at the longest pause.
"""

import argparse
import json
import statistics
from pathlib import Path

import metrics
from video_download import chunking


def planned_cuts(job: Path, speech, captions, use_chapters: bool) -> list[float]:
    info = json.loads((job / 'info.json').read_text())
    chapters = [(c['start_time'], c['title']) for c in info.get('chapters') or []] if use_chapters else []
    plan = chunking.plan_chunks(float(info['duration']), speech, chapters, None, captions)
    return [c.start for c in plan.chunks[1:]]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('captions', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--transcript', type=Path, help='a chunked .transcript.json')
    source.add_argument('--plan', type=Path, metavar='JOB_DIR', help='plan the chunks of this downloaded job')
    parser.add_argument('--speech', type=Path, help='speech regions from python -m video_download.vad, for --plan')
    parser.add_argument('--no-chapters', action='store_true', help='plan as if the video had no chapters')
    parser.add_argument('--no-captions', action='store_true', help='plan without scoring the pauses from the captions')
    args = parser.parse_args()
    captions = metrics.vtt_words(args.captions.read_text())
    if args.transcript:
        cuts = [start for start, _ in metrics.load_transcript(args.transcript).chunks[1:]]
    else:
        if not args.speech:
            parser.error('--plan needs --speech')
        speech = metrics.load_speech(args.speech)
        cuts = planned_cuts(args.plan, speech, None if args.no_captions else captions, not args.no_chapters)
    rows = [metrics.cut_quality(captions, cut) for cut in cuts]
    leads = [r['lead_seconds'] for r in rows if r['lead_seconds'] is not None]
    print(
        json.dumps(
            {
                'cuts': len(rows),
                'at_sentence_end_or_speaker_change': sum(r['boundary'] for r in rows),
                'mid_sentence': sum(not r['boundary'] for r in rows),
                'lead_below_0.5s': sum(lead < 0.5 for lead in leads),
                'median_lead_seconds': round(statistics.median(leads), 2) if leads else None,
            }
        )
    )
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
