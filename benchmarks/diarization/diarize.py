"""Time pyannote speaker diarization of one recording, stage by stage; writes diar.tsv and excl.tsv to out_dir, prints JSON.

    python diarize.py <audio.pcm> <out_dir> [--device cpu|cuda] [--threads N] [--embedding-batch N]

The audio is raw 16 kHz mono signed 16-bit PCM, decoded by ffmpeg beforehand, because torchcodec does not load on
every Python version pyannote runs on. Needs pyannote.audio and HF_TOKEN with access to the model; see run.sh and
docs/benchmark-methodology.md.
"""

import argparse
import json
import os
import resource
import time
from pathlib import Path

import numpy as np
import torch
from pyannote.audio import Pipeline
from pyannote.core import SlidingWindowFeature

MODEL = 'pyannote/speaker-diarization-community-1'
SAMPLE_RATE = 16000


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('pcm', type=Path)
    parser.add_argument('out_dir', type=Path)
    parser.add_argument('--device', default='cpu', choices=['cpu', 'cuda'])
    parser.add_argument('--threads', type=int, help='torch threads on the CPU, all cores by default')
    parser.add_argument('--embedding-batch', type=int, help="pyannote's embedding batch size, 32 by default")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.threads:
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
    cuda = args.device == 'cuda'

    def sync():
        if cuda:
            torch.cuda.synchronize()

    started = time.monotonic()
    pipe = Pipeline.from_pretrained(MODEL, token=os.environ['HF_TOKEN'])
    if args.embedding_batch:
        pipe.embedding_batch_size = args.embedding_batch
    pipe.to(torch.device(args.device))
    sync()
    samples = np.fromfile(args.pcm, dtype=np.int16).astype(np.float32) / 32768.0
    file = {'waveform': torch.from_numpy(samples)[None], 'sample_rate': SAMPLE_RATE, 'uri': args.pcm.stem}
    log = {
        'device': args.device,
        'cpu_capability': torch.backends.cpu.get_cpu_capability(),
        'threads': torch.get_num_threads(),
        'embedding_batch': pipe.embedding_batch_size,
        'audio_seconds': round(len(samples) / SAMPLE_RATE, 1),
        'load_seconds': round(time.monotonic() - started, 1),
    }

    # The stages of the pipeline's own apply(), timed one by one: the speaker embeddings dominate
    t = time.monotonic()
    segmentation = pipe.get_segmentations(file)
    sync()
    receptive_field = pipe._segmentation.model.receptive_field
    count = pipe.speaker_count(segmentation, receptive_field, warm_up=(0.0, 0.0))
    log['segmentation_seconds'] = round(time.monotonic() - t, 1)
    t = time.monotonic()
    embeddings = pipe.get_embeddings(file, segmentation, exclude_overlap=pipe.embedding_exclude_overlap)
    sync()
    log['embedding_seconds'] = round(time.monotonic() - t, 1)
    t = time.monotonic()
    hard, _, _ = pipe.clustering(
        embeddings=embeddings,
        segmentations=segmentation,
        num_clusters=None,
        min_clusters=1,
        max_clusters=np.inf,
        file=file,
        frames=receptive_field,
    )
    hard[np.sum(segmentation.data, axis=1) == 0] = -2
    # diar.tsv lets turns overlap where two people talk at once; excl.tsv keeps one speaker at a time, which is what a
    # transcript's lines can carry and what the speaker-change measures were published on
    outputs = {}
    for name, speakers in (('diar', count.data), ('excl', np.minimum(count.data, 1))):
        counts = SlidingWindowFeature(speakers.astype(np.int8), count.sliding_window)
        outputs[name] = pipe.to_annotation(
            pipe.reconstruct(segmentation, hard, counts),
            min_duration_on=0.0,
            min_duration_off=pipe.segmentation.min_duration_off,
        )
    log['clustering_seconds'] = round(time.monotonic() - t, 1)

    for name, result in outputs.items():
        with (args.out_dir / f'{name}.tsv').open('w') as f:
            for turn, _, label in result.itertracks(yield_label=True):
                f.write(f'{turn.start:.3f}\t{turn.end:.3f}\tS{label}\n')
    annotation = outputs['diar']
    work = log['segmentation_seconds'] + log['embedding_seconds'] + log['clustering_seconds']
    log['real_time_factor'] = round(work / log['audio_seconds'], 4)
    log['speakers'] = {f'S{label}': round(annotation.label_duration(label)) for label in annotation.labels()}
    log['peak_rss_mb'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    if cuda:
        log['peak_gpu_mb'] = torch.cuda.max_memory_reserved() // 2**20
    print(json.dumps(log))


if __name__ == '__main__':
    main()
