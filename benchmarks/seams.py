"""Words lost and passages duplicated at the chunk seams of a chunked transcript; prints JSON, one line per seam.

    python benchmarks/seams.py <captions.vtt> <transcript.json> [--at SECONDS ...] [--window 5]

The seams are the chunk starts recorded in the service's .transcript.json; --at gives them explicitly for a transcript
without chunk records.
"""

import argparse
import json
from pathlib import Path

import metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('captions', type=Path)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--at', type=float, nargs='+', help='seam times in seconds, instead of the chunk starts')
    parser.add_argument('--window', type=float, default=5.0, help='seconds on each side of the seam (default 5)')
    parser.add_argument(
        '--drop-annotations', action='store_true', help='leave [music] and the like out of the captions'
    )
    args = parser.parse_args()
    ref = metrics.reference_words(args.captions.read_text(), args.drop_annotations)
    transcript = metrics.load_transcript(args.transcript)
    seams = args.at or [start for start, _ in transcript.chunks[1:]]
    if not seams:
        parser.error('the transcript has no chunks; give the seams with --at')
    rows = [metrics.seam_loss(ref, transcript.segments, seam, args.window) for seam in seams]
    total = {
        'seams': len(rows),
        'caption_words': sum(r['caption_words'] for r in rows),
        'lost': sum(r['lost'] for r in rows),
        'duplicated_trigrams': sum(r['duplicated_trigrams'] for r in rows),
    }
    print(json.dumps(total))
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
