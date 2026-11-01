#!/bin/bash
# Run diarize.py in a pyannote environment, optionally pinned to some cores and limited to older instruction sets.
#
#   run.sh <cpus|all> <native|no-avx2> <audio.pcm> <out_dir> [diarize.py options]
#
# cpus is a taskset list such as 0,1; no-avx2 makes PyTorch, oneDNN and MKL all use AVX at most, as on a CPU without
# AVX2. PyTorch alone (ATEN_CPU_CAPABILITY=default) is not enough: oneDNN and MKL pick their own instruction sets.
# HF_TOKEN must be set; VENV points at the environment with pyannote.audio (default: .venv next to this script).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cpus=$1 isa=$2
shift 2
: "${HF_TOKEN:?HF_TOKEN must be set to a Hugging Face token with access to the pyannote model}"
venv=${VENV:-$here/.venv}

if [ "$isa" = no-avx2 ]; then
    export ATEN_CPU_CAPABILITY=default ONEDNN_MAX_CPU_ISA=AVX MKL_ENABLE_INSTRUCTIONS=AVX
fi
pin=()
if [ "$cpus" != all ]; then
    pin=(taskset -c "$cpus")
    threads=$(( $(tr ',' '\n' <<< "$cpus" | wc -l) ))
    export OMP_NUM_THREADS=$threads MKL_NUM_THREADS=$threads
fi

# 2.4-2.8 GB is normal; the cap keeps a runaway run from taking the desktop session with it
exec systemd-run --user --scope -q -p MemoryMax=10G -p MemorySwapMax=0 \
    "${pin[@]}" "$venv/bin/python" -I "$here/diarize.py" "$@"
