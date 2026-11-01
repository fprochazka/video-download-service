# Transcription benchmarks: how they are measured

How the numbers in [transcription-models.md](transcription-models.md) and [transcription-chunking.md](transcription-chunking.md) were measured, and how to measure a new provider, model or pipeline change the same way. The scripts are in [`benchmarks/`](../benchmarks); [Running the scripts](#running-the-scripts) shows the commands.

Every measure compares a transcript with YouTube's own captions of the same recording. Both become lists of normalized words with a start time, `difflib.SequenceMatcher` aligns the two lists, and each measure reads that alignment. Most measures need no API call, so a transcript that was paid for once can be scored again whenever a measure changes.

## Test recordings

All are public YouTube videos. Download them through the service, which saves the video, `info.json` and the subtitles into one job directory, or with yt-dlp (`--write-info-json --write-auto-subs --sub-langs en-orig --sub-format vtt`, `cs-orig` for the Czech one).

- **English short:** ["10x engineer (AI Psychosis)"](https://www.youtube.com/watch?v=WV6A1Zf3F2Q), 357 s, two speakers, manual English subtitles (`en`). The model and output-format comparison ran on it in one request per run.
- **English long:** ["Mark Cuban: The Brutal Truth About The Next Market Crash!"](https://youtu.be/6VTSAs4Rsck), 5493 s, an interview with two speakers, an intro montage and ads, 33 chapters, English speech recognition captions (`en-orig`). All chunking experiments and the ElevenLabs and pyannote comparisons ran on it.
- **Czech:** ["Aleš Vávra: Lidi neznají krizi…"](https://www.youtube.com/watch?v=RbEMlbGmZrU), 3840 s, 22 chapters, Czech speech recognition captions (`cs-orig`).
- **Smoke test:** ["Me at the zoo"](https://www.youtube.com/watch?v=jNQXAC9IVRw), 19 s. It only shows that a provider and the whole service path work end to end; it is too short to measure anything.

## The ground truth: YouTube's speech recognition captions

The `*-orig.vtt` track is YouTube's own speech recognition in the spoken language. It is the best reference that exists for free on every video, but it is not a human transcript:

- **It is speech recognition itself.** A few percent of "missed" words are caption errors or spelling differences, so no transcript scores 100%, and 97–98% recall is about the ceiling on English. Compare providers with each other on the same captions, never with an absolute target.
- **It contains annotations.** `[music]`, `[applause]`, `[laughter]` are caption words. A transcript that leaves them out loses recall for them. The published numbers keep them; `--drop-annotations` leaves them out.
- **Every word has a start time.** Each cue repeats the previous line as the text rolls up, and only the line with `<00:01:02.345><c> word</c>` tags is new. The service's `vtt_words()` reads only those lines, so each word appears once with its own time. Manual subtitles (`en`) have no word times; their words are spread evenly over the cue, which makes drift on the English short recording coarser.
- **`>>` marks a speaker change**, HTML-escaped as `&gt;&gt;` in the file. It marks that someone else starts talking, not who. On the English long recording it occurs 408 times. The markers miss some changes, especially short interjections, so a precision below 1 against them is partly the markers' fault.
- **The captions carry punctuation.** A sentence end is a word ending in `.`, `?`, `!` or `…`. The cut-quality proxy and the service's cut scoring rely on it.

## Text normalization

Both sides go through the service's `chunking.words()`: lowercase, apostrophes removed (`don't` becomes `dont`), then every run of word characters is a word. Punctuation, speaker labels and timestamps drop out. Using the service's function means a benchmark counts words the same way as the pipeline's own coverage check.

"Normalized" measures go one step further, because providers spell the same speech differently:

- Spoken forms are spelled out: `gonna` becomes `going to`, `wanna` becomes `want to`, `cause` and `cuz` become `because`, Czech `todle` becomes `tohle`.
- Numbers are left out, both digits and number words, in English and Czech. The captions write "66", Scribe writes "sixty six", and neither is wrong.

Raw and normalized recall differ by up to 2 points for a provider that writes numbers as words: Scribe's English recall is 96.0% raw and 97.8% normalized, Gemini's 97.8% and 97.9%. Publish the normalized value when comparing providers.

## Measures

### Recall and insertions

Recall is the share of caption words that the alignment matches to a transcript word, in order. It answers "how much of the speech is in the transcript". Insertions are the share of transcript words that match no caption word: fillers the captions leave out, made-up speech, a passage transcribed twice. Read the two together. A model that repeats a passage keeps its recall and raises its insertions.

`autojunk` is off in the alignment. With it on, difflib ignores the most frequent words ("the", "a") on any recording longer than a few minutes and reports a recall far too low.

### Timestamp drift at anchors

Drift is the transcript's start time minus the captions' time of the same word. It is measured only at anchors: words inside a run of at least 4 matched words in a row, one anchor at most every 20 s. A run of four words in the same order is the same passage, while a single common word can match anywhere. The tool reports the median, 95th percentile and maximum of the absolute drift, and the worst anchor with its time.

A Gemini segment has one timestamp for the whole line, so every word of the line carries the line's start. That adds a few seconds of drift towards the end of a long line, so a median of 1–3 s is normal for Gemini. The maximum is the useful number: it shows a chunk that is minutes late.

The 6-minute model comparison in transcription-models.md used 8 anchors spread evenly over the recording, in runs of at least 3 words. The script uses the method above, which also works on long recordings.

### Word-level timing

For a provider with a time per word, such as ElevenLabs Scribe, every aligned word gets its offset from the caption word. The tool reports the median, 90th and 99th percentiles, and the shares within 0.3 s and 1 s. Scribe had 99.4% of English words within 0.3 s.

### Blind gaps

A skipped passage shows as a long stretch of speech with no transcript line. The tool measures it in two ways:

- **Blind speech** (`--speech`): the most detected speech between two consecutive segment starts, the same check the pipeline runs on each chunk answer. It depends on the voice activity detector, which also counts sounds that are not words: Scribe, which skipped nothing, still shows 20.2 s on the English long recording. Use it to compare two transcripts on the same speech regions, never as an absolute value.
- **Longest unmatched caption run**: the longest stretch of captions, in seconds, with at least 5 caption words and none of them in the transcript. It needs no detector. It found the 76-word passage (19.2 s at 4:15) that one Gemini run lost at a seam; Scribe's longest was 2.2 s.

### Words lost in long runs

This is the sum of caption words deleted or replaced in runs of at least 8 words. Single missed words are mostly spelling and caption differences, while a run of 8 or more is a lost passage. It separates "drops a word here and there" from "drops sentences".

### Seam loss

For every chunk seam except the first chunk's start, the tool aligns the captions of ±20 s around the seam with the transcript lines that start within ±30 s. It then counts the caption words within ±5 s of the seam that stayed unmatched. Duplicated trigrams count three-word phrases that occur both in the 15 s before the seam and in the 15 s after it: a passage transcribed by both chunks. The local alignment keeps a loss elsewhere in the recording from shifting the count. The ±5 s window is wide enough for Gemini's drift and narrow enough to blame the seam.

The per-answer variant in the lead-in and padding experiments counted only the 5 s after the cut, per chunk answer, with annotations dropped ("words lost per chunk start"). It needed the experiment's saved answers and its chunk list, so it is not in `benchmarks/`; `seams.py --window` covers the same question on a finished transcript.

### Repeated lines

This counts distinct lines of 4 or more words that occur more than once, the service's `repeated_lines()`. Dialogue rarely repeats a whole line, while a model caught in a loop does. Scribe's English transcript of 91 minutes has 8 such lines; Gemini Flash-Lite in JSON mode had 9 on 6 minutes.

### End gap

The end gap is the recording's length minus the end of the last segment. An answer that stopped early has a large one. On a chunked transcript the per-chunk version of this is the pipeline's own `stopped_early` check, so the whole-file number only catches the last chunk.

### Cut-quality proxy

This measure judges where the chunk cuts fall without any transcription call. For each cut, it looks at the caption word before the cut and the one after. The cut is at a boundary when the word before ends a sentence or the word after starts a `>>` turn; otherwise it is mid-sentence. The lead is the time from the cut to the next caption word, and a lead under 0.5 s means the cut is close to clipping the next word. `cuts.py --plan` runs the service's current chunk planner on a downloaded job, so a planner change can be judged for free before any money is spent on it.

The proxy predicts readability, not lost words. Better cuts made the joined transcript read better but did not reduce the words lost at seams, see transcription-chunking.md.

### Speaker changes

A speaker change of the transcript is a word whose speaker label differs from the previous word's. It gets the caption time of the next aligned word, at most 5 words on, so the transcript's own timestamp error does not count. Each `>>` marker can match one change within ±1 s. Precision is the share of changes that match a marker, recall the share of markers that match a change. Label restarts at chunk seams do not matter here, because only the fact of a change counts.

On the English long recording Scribe had 93% recall at 52% precision, and Gemini 82% recall at 63% precision. Scribe keeps backchannels as turns of their own (see transcription-models.md), and the captions mark few of them.

For a diarization without words, such as pyannote's output, `diarization_quality.py` uses the span from the end of one label's turn to the start of the next label's turn, and a marker within 1 s of that span matches.

### Turn purity and label agreement

**Turn purity** splits the captions into turns at the `>>` markers. In each turn with at least 15 aligned words, it takes the share of words that carry the turn's most frequent label, weighted by words. Purity shows whether one person's turn keeps one label. Alternation is the share of consecutive turns whose majority labels differ, and it should be high in an interview. For a chunked transcript a label counts per chunk, and turn pairs across a seam are skipped.

**Label agreement** compares two transcripts' labels word by word, after aligning the two transcripts with each other. Put the chunked one first. Each of its labels maps to the label of the other transcript that it most often coincides with. The mapping is computed once for the whole file (global) and again inside each chunk (per chunk). Gemini restarts its labels in every chunk, so "Speaker 1" is the host in one chunk and the guest in the next. The global mapping measures what a reader gets, the per-chunk mapping what the model got right inside each request, and the gap between the two is what the restarts cost. Against Scribe on the English long recording it was 72.5% global and 93.5% per chunk.

### Czech: diacritics and colloquial forms

- **Diacritics share** is the share of words with a Czech diacritic (`áčďéěíňóřšťúůýž`), in the captions and in the transcript. A transcript that drops diacritics reads as broken Czech and shows a share far below the captions' 39%.
- **Colloquial forms** count spoken Czech that a model may standardize: the `-ej` endings (`kterej`, `nějakej`), `todle`/`tadle`, `bysme`, `jo`. Transcribing colloquial forms as spoken is the goal. Gemini kept `kterej` 25 times against 25 in the captions, and Scribe turned 12 of 13 `todle` into `tohle`.

### Numbers

The tool counts digit tokens and number words in the captions and in the transcript. It does not score them, because both forms are correct. It shows the style that a reader gets: Scribe writes "two thousand and twenty two", and the captions write "2022".

## Cost accounting

The cost of a call is its usage metadata times the list price. The service's `pricing.request_cost()` computes it, and the tools use the same function:

- **Gemini:** `usage_metadata.prompt_token_count` (with the audio share from `prompt_tokens_details`), `candidates_token_count` and `thoughts_token_count` from the answer, times the Developer API standard-tier prices in `pricing.py`. Thinking tokens bill at the output price.
- **Never use `countTokens` for audio.** It reports 32 tokens per second, while billing counts 25 per second. An estimate from it is 28% too high.
- **ElevenLabs:** the response headers carry the cost of each request: `character-cost` in the account's usage units (about 10 per minute of audio) and `fiat-cost-before-overages` with `fiat-currency`. The service prices Scribe per hour of audio from the public price list instead.

The service records each request's usage in the job's `metadata.json`, so the cost of any stored transcription can be recomputed after a price change.

## Replaying saved answers

`pipeline.py record` runs the service's real Gemini pipeline on a job and saves each request's audio, prompt and raw answer. `pipeline.py replay` then runs the current pipeline again with those answers in place of Gemini. The replay costs nothing and gives a full transcript and `metadata.json`, so a change to the checks, the timestamp repairs or the stitching can be measured on answers that were already paid for. The replay of a recorded run on the English long recording reproduced the run's recorded cost, $0.190131.

An answer is picked by the chunk index and start second in the chunk's audio file name. A chunk that the changed code cuts differently has no saved answer and fails. A chunk that the current checks reject now, though they passed it then, asks for a retry that was never recorded. The replay prints these as `chunks_without_answer`. Several calls directories can be given; their answers for the same chunk are used in order, which makes a retry draw a different saved answer.

The limits of the checks were chosen this way: for example, the early-stop limit of 24 s of speech after the last line came from replaying all saved answers with limits of 15, 20, 24 and 30 s.

## Run-to-run variance

Gemini does not return the same answer twice for the same request. On the English long recording, the same chunk sent four times lost 0–2 words after its start in different answers. Of 36 answers to 9 chunks, 4 failed a check that the other answers to the same chunk passed. The chunk with the intro montage had a digit slip and early stops in some runs and none in others.

Rules that follow from this:

- A difference of a few words, or of less than half a point of recall, between two single runs is noise.
- To compare two variants of a chunk request, send each chunk at least 3 times per variant and compare totals over all answers. The lead-in experiment used 9 chunks × 3 answers per variant.
- A failure rate needs dozens of answers. 5 failed answers of 30 is a rate between roughly 5% and 35%.
- Scribe was run once per recording. Its timestamps and labels come from one pass over the whole file, so its variance is expected to be lower, but that was not measured.

## The spend ledger

Every experiment that calls a paid API keeps a ledger: a Markdown table with one row per call, its tokens, its cost and the running total, plus a cap written at the top before the first call. The calling script reads the total before each call and refuses to start a call once the total reaches the cap. Each experiment gets its own ledger file, so the cap applies to that experiment's new spend.

The ledger exists because the costs hide in retries. A pipeline run on the long recording makes 23 requests or more, and a broken prompt makes every chunk retry and split. The cap stops such a run at a known amount, and the table shows which call cost what. `pipeline.py record` requires `--ledger` and `--cap`, and without `--spend` it only prints its estimate.

## Diarization benchmark

`benchmarks/diarization/diarize.py` runs `pyannote/speaker-diarization-community-1` stage by stage (segmentation, speaker embeddings, clustering) and times each stage. It writes two label files: `diar.tsv` allows overlapping turns, and `excl.tsv` keeps one speaker at a time. The published speaker-change numbers were measured on `excl.tsv`. `diarization_quality.py` scores either file against the `>>` markers.

The published speeds came from three machines:

- an 8-core desktop CPU with AVX-512;
- a consumer GPU from 2016 with 6 GB;
- an emulated 2-core CPU without AVX2.

`run.sh` does the emulation:

- `taskset -c 0,1` pins the process to two cores, and `OMP_NUM_THREADS` and `MKL_NUM_THREADS` match the core count.
- `ATEN_CPU_CAPABILITY=default` stops PyTorch's own kernels from using AVX2 or AVX-512.
- `ONEDNN_MAX_CPU_ISA=AVX` and `MKL_ENABLE_INSTRUCTIONS=AVX` limit oneDNN and MKL, which choose their instruction sets independently of PyTorch. Without these two variables the "emulated" run still uses the newer instructions inside those libraries.

`diarize.py` logs `torch.backends.cpu.get_cpu_capability()`, which shows what PyTorch picked. A real older 2-core laptop CPU also has a lower clock and less cache, and is another 1.5–2× slower than the emulation. On a slow configuration, time a 10-minute excerpt and extrapolate. The embeddings take 95% of the time and scale linearly with the audio length.

The scorer counts each caption turn once (408 markers on the English long recording) and gets 67% of markers with a change and 74% of changes with a marker on `excl.tsv`. The published 66% and 78% came from an earlier parser that counted 512 markers, because it took markers from repeated caption lines as well.

## Running the scripts

The scripts run with the service's environment and import its code, so they normalize and parse exactly like the pipeline. Set the environment up once:

```bash
cd service && uv sync
```

Then run each script from the repository root with the service's Python, `service/.venv/bin/python benchmarks/<script>.py`. The scripts print JSON and take a few seconds on a 90-minute recording.

Speech regions for the blind gaps and the cut planner come from the service's voice activity detector, on audio in the service's format:

```bash
ffmpeg -i video.mp4 -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k -application voip audio.ogg
(cd service && .venv/bin/python -m video_download.vad ../audio.ogg) > speech.json
```

Score a transcript. It can be the service's `.transcript.json` or `.transcript.md`, a raw Scribe answer, or a JSON list of `{start, speaker, text}`:

```bash
service/.venv/bin/python benchmarks/score.py video.en-orig.vtt video.transcript.json --speech speech.json
service/.venv/bin/python benchmarks/score.py video.cs-orig.vtt scribe.json --czech
```

Seam loss of a chunked transcript, cut quality of a transcript or of the current planner, and label agreement:

```bash
service/.venv/bin/python benchmarks/seams.py video.en-orig.vtt video.transcript.json
service/.venv/bin/python benchmarks/cuts.py video.en-orig.vtt --transcript video.transcript.json
service/.venv/bin/python benchmarks/cuts.py video.en-orig.vtt --plan downloads/<job_id> --speech speech.json [--no-chapters] [--no-captions]
service/.venv/bin/python benchmarks/labels.py gemini.transcript.json scribe.json
```

Record and replay the Gemini pipeline. Run both on a copy of the job directory, because both write the transcript into it. `record` prints its estimate and stops unless `--spend` is given:

```bash
TRANSCRIPTION_PROVIDER=gemini GEMINI_API_KEY=... service/.venv/bin/python benchmarks/pipeline.py \
    record copy-of-downloads <job_id> <media file> calls/run1 --ledger ledger.md --cap 1.00 [--spend]
service/.venv/bin/python benchmarks/pipeline.py replay copy-of-downloads <job_id> <media file> calls/run1
```

To score a new provider, save its raw answer, convert it to a JSON list of `{start, speaker, text}` segments (one per sentence or line, `start` in seconds from the start of the recording), and score that list. A provider with word times can use Scribe's shape instead, `{"words": [{"type": "word", "text", "start", "speaker_id"}]}`, which also enables the word-timing measure.

The diarization benchmark needs its own environment with pyannote and a Hugging Face token that has accepted the model's terms:

```bash
uv venv --python 3.14 benchmarks/diarization/.venv
uv pip install --python benchmarks/diarization/.venv pyannote.audio==4.0.7
ffmpeg -i video.mp4 -vn -ac 1 -ar 16000 -f s16le audio.pcm
HF_TOKEN=... benchmarks/diarization/run.sh all native audio.pcm out/native
HF_TOKEN=... benchmarks/diarization/run.sh 0,1 no-avx2 audio.pcm out/emulated
HF_TOKEN=... benchmarks/diarization/run.sh all native audio.pcm out/gpu --device cuda
service/.venv/bin/python benchmarks/diarization_quality.py video.en-orig.vtt out/native/excl.tsv
```

For an older GPU, install PyTorch from the CUDA 12.6 index (`--index-url https://download.pytorch.org/whl/cu126`); newer CUDA builds may lack its kernels.

`benchmarks/test_metrics.py` tests the measures on small synthetic captions, and CI runs it with the service's tests.
