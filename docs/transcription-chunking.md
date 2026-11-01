# Transcription chunking: what we measured

How long recordings are cut into chunks for Gemini, and which ideas helped or hurt. The test recordings and the ground truth are described in [transcription-models.md](transcription-models.md). Every experiment ran on `gemini-3.5-flash-lite` with plain-text output, and most ran once per variant, so a difference of a few words is within the run-to-run variance. [benchmark-methodology.md](benchmark-methodology.md) defines each measure and has the scripts that reproduce them.

## Current design

- A recording under 7 minutes goes to Gemini in one request.
- A longer one is cut into chunks of about 5 minutes, at least 0.4× and at most 1.4× that target.
- With chapters, neighboring chapters are merged until the next one would exceed the target, and each boundary snaps to the best-scoring pause within ±15 s, then ±45 s. Without chapters, the cut goes to the best pause within ±60 s, then ±120 s, of each target.
- Pauses come from Silero VAD (`pysilero-vad`), with ffmpeg `silencedetect` and then a flagged hard cut as fallbacks.
- Each pause is scored from the subtitles: a sentence end before it, a speaker change (`>>`) after it, its length, a margin before the next word, and its distance from the target.
- Chunks run in parallel, each as its own audio file with no overlap and no padding, and are joined by offsetting their timestamps.
- Each answer passes local checks (finish reason, audio tokens, monotonic timestamps, gaps, coverage against the subtitles, repeats, script). A failing chunk is sent again, then split in two, then the transcription fails.

## Chunk length

| Chunk length | Recording | Result |
|---|---|---|
| 7–10 min | English long | 12 of 22 answers stopped early, skipped passages or made up speech. Two invented an ending of the interview ("Mark, this has been incredible"). |
| 2–7 min (target 5) | English long | 5 of 30 answers failed the checks; every retry passed. 97.8% coverage, 14 s max drift, $0.22 for 91 min. |
| ~120 s | English long, first 30 min | 98.0% coverage, no failures, the same cost as 5 min, but twice the seams. |
| ~60 s | English long, first 30 min | 98.1% coverage, 20% more expensive, 13 real words lost at 35 seams against 1 at 7 seams with 5 min. |

Shorter chunks prevent the large failures: no early stops, no minutes-late answers, smaller gaps. They do not reduce the words lost per seam, which stays at about 0.4–0.8, so more seams lose more words. Speaker labels restart in every chunk, so short chunks also flip the host and guest labels more often. In 15 of 36 sixty-second chunks there was too little dialogue to tell who the host is.

## Where to cut

On the English long recording, measured on the cuts without any Gemini calls:

| Cut placement | Cuts at a sentence end or speaker change | Mid-sentence cuts |
|---|---|---|
| Chapters + longest pause | 12 of 22 | 10 |
| Chapters + pause scored from subtitles | 15 of 21 | 6 |
| No chapters + longest pause | 9 of 18 | 9 |
| No chapters + pause scored from subtitles | 15 of 18 | 3 |

Better cut placement made the joined transcript read better but did not reduce lost words. Of 7 mid-sentence cuts, 6 lost nothing. The lost words sit 0.4–3.5 s after cuts that already fall at a sentence end: short interjections and repeats at speaker turns ("No,", "I'm having", parts of numbers). Some of them disappear on every resend of the same chunk, so they depend on that audio, not on the cut.

YouTube's English and Czech speech-recognition captions carry punctuation and `>>` speaker-change markers (HTML-escaped as `&gt;&gt;` in the VTT), with a start time per word. A sentence end or speaker change followed by a pause occurs about every 12 s; the largest gap between two of them was 91 s. Most pauses are only 0.3–0.6 s long.

## Ideas that made things worse

| Variant | Answers passing | Words lost per chunk start | Duplicated words |
|---|---|---|---|
| Cut exactly at the pause (current) | 32 of 36 | 0.44 | 0 |
| 3 s of real audio before the cut, prompt says not to transcribe it | 23 of 27 | 1.87 | 75 |
| 3 s of real audio before the cut, lines before 3 s dropped | 27 of 27 | 0.93 | 0 |
| 2 s of silence before and after each chunk | 18 of 27 | 0.22 with lines in the padding kept, 4.2 with them dropped | 0 |
| 2 s of silence after each chunk only | 7 of 8 | 0.14 | 0 |

- **Lead-in audio** gives the model speech it must skip; it either transcribed it anyway (duplicates) or lost more words.
- **Silence before the chunk** broke timestamps: 9 of 27 answers wrote every timestamp 2–3 minutes late, and Gemini did not count the padding consistently (5 of 18 passing answers put their first real line inside it), so the stitcher cannot tell padding lines from speech. Gemini never wrote anything into the silence itself.
- **Silence after the chunk only** caused no timestamp shift but did not fix early stops, and one answer stamped its last 8 real lines inside the padding.
- The padded audio was checked: exact durations, digital silence, speech aligned at exactly 2.000 s against the same range cut from the full recording. The failures are Gemini's reaction, not a broken file.

## Timestamp failures and their repairs

- **Whole minutes late:** some answers write `[00:01:05]` for the fifth second on every line. The service moves such an answer back by whole minutes (10 of 208 saved answers had this shape).
- **Digit slip:** an answer jumped from `[00:00:59]` to `[00:10:04]` at a line the captions put at 1:04, where music starts under the speech, and the rest of the answer followed the wrong value. The service moves the lines after a jump back when the jump matches a slipped minute or hour digit and the moved lines then fit the chunk. Shifting by any whole number of minutes accepted made-up jumps, so it is limited to digit slips.
- **Early stop:** an answer can stop 26 s before the chunk's end with no visible cause in the audio. The service sends the chunk again when more than 24 s of speech come after the last line. Good answers had at most 20.4 s (English) and 16.5 s (Czech) there. A gap limit between two lines of 15 s would have failed 67 good answers, and 24 s failed good Czech answers with 28–30 s lines, so that limit stays at 30 s.
- **Not caught:** an answer from an older prompt transcribed speech from two minutes later (5.6% coverage) and passed every check.

## Chunk 0 of the English long recording

The first chunk holds a 90 s montage of clips cut together from across the interview, a music bed from 1:03, about 14 s of music, the host's "please subscribe" request recorded separately, and a jingle before the interview starts at 2:12. It produced the digit slip and the early stops above in some runs and none in others. Cutting the intro chapter as its own chunk is untested.
