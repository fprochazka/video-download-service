"""Speaker-label agreement between two transcripts of the same recording; prints JSON.

    python benchmarks/labels.py <transcript_a> <transcript_b>

Put the chunked transcript first: its labels are mapped onto the other's once for the whole file and once per chunk,
and the difference shows what the label restarts at chunk seams cost. Both take any format score.py reads.
"""

import argparse
import json
from pathlib import Path

import metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('transcript_a', type=Path)
    parser.add_argument('transcript_b', type=Path)
    args = parser.parse_args()
    a, b = metrics.load_transcript(args.transcript_a), metrics.load_transcript(args.transcript_b)
    print(json.dumps(metrics.label_agreement(a, b)))


if __name__ == '__main__':
    main()
