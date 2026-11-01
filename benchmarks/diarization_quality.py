"""Speaker changes of a diarization (diarization/diarize.py's diar.tsv) against the captions' >> markers; prints JSON.

    python benchmarks/diarization_quality.py <captions.vtt> <diar.tsv> [--tolerance 1.0]

Also prints each label's speaking seconds per 10 minutes, which shows whether one person keeps one label.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import metrics


def read_turns(path: Path) -> list[tuple[float, float, str]]:
    rows = [line.split('\t') for line in path.read_text().splitlines() if line]
    return [(float(start), float(end), label) for start, end, label in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('captions', type=Path)
    parser.add_argument('tsv', type=Path)
    parser.add_argument('--tolerance', type=float, default=1.0)
    args = parser.parse_args()
    markers = sorted({c.time for c in metrics.vtt_words(args.captions.read_text()) if c.speaker_change})
    turns = read_turns(args.tsv)
    print(json.dumps(metrics.diarization_changes(turns, markers, args.tolerance)))
    for window in range(0, int(turns[-1][1]) + 1, 600):
        seconds = Counter()
        for start, end, label in turns:
            overlap = min(end, window + 600) - max(start, window)
            if overlap > 0:
                seconds[label] += overlap
        print(
            json.dumps(
                {'minutes': f'{window // 60}-{window // 60 + 10}'} | {k: round(v) for k, v in sorted(seconds.items())}
            )
        )


if __name__ == '__main__':
    main()
