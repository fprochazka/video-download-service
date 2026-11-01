# Transcription models: what we measured

Which Gemini model and request shape transcribe best, measured on our own recordings. All prices are list prices of the Gemini Developer API (AI Studio), standard tier, read in October and November 2026. The test recordings:

- **English short:** "10x engineer (AI Psychosis)", 357 s, two speakers, manual English subtitles.
- **English long:** "Mark Cuban: The Brutal Truth About The Next Market Crash!", 5493 s (91.5 min), interview with two speakers, an intro montage and ads, 33 chapters, YouTube's English speech-recognition captions (`en-orig`).
- **Czech:** "Aleš Vávra: Lidi neznají krizi…", 3840 s (64 min), YouTube's Czech speech-recognition captions (`cs-orig`), 22 chapters.

The captions serve as ground truth. They are YouTube's own speech recognition, so they are good but not perfect: a few percent of "missed" words are caption or spelling differences.

## Decision

`gemini-3.5-flash-lite`, plain-text output, no response schema. It is the cheapest model, about $0.12–0.15 per hour of audio, and no other model covered more of the speech. Bigger models only keep timestamps tighter.

## Plain text, never a JSON response schema

On the English short recording, one request per run:

| Model, output | Caption words covered | Repeated lines | Max timestamp drift | Cost |
|---|---|---|---|---|
| 3.5 Flash-Lite, JSON schema | 66% | 9 | 120 s | $0.020 |
| 3.5 Flash, JSON schema | 66% | 0 | 120 s | ~$0.14 |
| 3.1 Pro preview, JSON schema | 77% | 0 | 81 s | $0.10 |
| 3.5 Transcribe | 93% | 0 | 6.8 s | $0.036 |
| 3.5 Flash-Lite, JSON schema, untimed subtitles | 96% | 13 | 282 s | $0.025 |
| 3.5 Flash-Lite, plain text, untimed subtitles | 96% | 0 | 1.7 s | $0.013 |
| 3.5 Flash-Lite, plain text, no subtitles | 94% | 0 | 1.1 s | $0.012 |

- The JSON response mode makes Flash-Lite loop and drift. With the same input, plain text was clean. Flash and Pro in JSON mode did not loop, but dropped a quarter to a third of the speech and spread the rest over the full length.
- Gemini rejects `pattern`, `minLength`, `maxLength` and a large `maxItems` in a response schema with a bare 400 INVALID_ARGUMENT. That cost four calls to find out.
- Subtitles in the prompt must go in without their timestamps. With timed subtitles the model copied the subtitles' times into the transcript. Without any subtitles coverage is still 94%, so the model listens to the audio and the subtitles add about two points.
- 3.5 Transcribe computes timestamps itself and labels speakers, but takes no prompt context (no title or description to spell names) and limits speaker labels to 30 minutes.

## Bigger models on chunks

Six chunks, 17 minutes of audio, three from the English long and three from the Czech recording, plain text as the pipeline sends it:

| Model | Answers passing the checks | Coverage en / cs | Max drift | Thinking tokens | $ per hour |
|---|---|---|---|---|---|
| 3.5 Flash-Lite | 10 of 10 | 94.5–98% / 92–94.5% | 7–15 s | 0 | 0.12 |
| 3.8 Flash | 5 of 6 | 94–98% / 92–95% | 3–13 s | 0–1665 | 0.26 (price doubles on 2027-01-01) |
| 3.1 Pro preview, thinking low | 3 of 3 | 94.5–98% / 94% | 5–6 s | 1834–2854 | 1.28 |
| 4 Argon, thinking low | 0 of 2 | 96% / 96% | 3 s en, 57 s cs | 0 | 0.63 (doubles on 2026-12-01) |

- Words lost in the first and last 5 s of a chunk were the same for every model. They come from caption and spelling differences, not from the model.
- 4 Argon sends 30 audio tokens per second, which the service's audio-token check rejects. On Czech it wrote only 12 coarse lines.
- There is no `gemini-3.5-pro`. The only Pro model that accepts audio is `gemini-3.1-pro-preview`.
- There is no 3.8 Flash-Lite. `gemini-3.8-flash-lite-tts` is a text-to-speech model.

## ElevenLabs Scribe v2 on whole recordings

One request per recording, no chunking: `scribe_v2` with speaker labels, word timestamps and audio-event tags. Price $0.22 per hour of audio on every plan; the account counted about 10 usage units per minute.

| Measure | English long: Gemini / Scribe | Czech: Gemini / Scribe |
|---|---|---|
| Caption words covered, after normalizing spelling | 97.9% / 97.8% | 95.5% / 93.6% |
| Text not in the captions | 2.9% / 5.1% | 5.5% / 8% |
| Timestamp drift, median / max | 1.8 s / 16.6 s / 0.11 s / 0.6 s | 2.9 s / 28.3 s / 0.03 s / 1.0 s |
| Longest stretch of speech with no transcript text | 19 s / 2.2 s | 5.5 s / 4.1 s |
| Speaker labels | restart in each of 22 chunks / 2 for the whole file | restart per chunk / 2 for the whole file |
| Cost | $0.19 / ~$0.34 | $0.15 / ~$0.23 |
| Wall time | 206 s / 64 s | 212 s / 181 s |

- Scribe's timestamps are exact (99.4% of English words within 0.3 s of the captions) and it never dropped a passage. Gemini lost a 76-word passage at one seam in the same run.
- Scribe keeps one speaker label per person across the whole file. Matching Gemini's labels with one mapping for the whole file agreed only 72–76% of the time, against 90–94% when the mapping is redone per chunk, so the label restarts alone cost about 20 points.
- Scribe writes numbers as words ("šedesát šest procent", "two thousand and twenty two") and "gonna", "'cause". In Czech this hurts readability most.
- Scribe keeps fillers, backchannels and cut-off words. The English transcript has 154 lines that are only "Mm-hmm" or "Yeah", against Gemini's 58.
- In Czech Scribe got several phrases right that Gemini garbled ("proti předchozímu časový Fedovi", "o těch scamech"), standardized some colloquial forms ("todle" becomes "tohle" in 12 of 13 cases) and swapped in a wrong place name (Réunion for Rujána).
- Audio events come in the recording's language: `[laughs]`, `[gentle music]`, `[smích]`, `[hudba]`.
- The service offers Scribe as its second provider, `TRANSCRIPTION_PROVIDER=elevenlabs`. Its Markdown puts the short interjections between two long turns of the other speaker in parentheses (96 of the English interview's turns) and writes English numbers in digits; Czech numbers stay words, see [the service README](../service/README.md#elevenlabs-scribe).

## Gotchas of the Gemini API

- **Billing counts 25 audio tokens per second.** `countTokens` reports 32 per second, a leftover of the Gemini 2.x formula, so never use it to estimate cost. The usage metadata of the answer is what is billed.
- **Temperature, top_p and top_k are ignored** from 3.6 Flash and 3.5 Flash-Lite on, and later models will return 400 for them. The service sends no sampling parameters.
- **Thinking stays at the model default.** On Flash-Lite that produced 0 thinking tokens; setting `low` on a failing chunk changed nothing.
- **The SDK does not retry by default.** Retries for 429 and 5xx need `HttpRetryOptions`.
- **Clip offsets (`videoMetadata`) are documented for video only.** Each chunk goes as its own small audio file inline in the request instead.
- **The language hint belongs in the system instruction.** At the start of the prompt it made one 112 s chunk write every timestamp a minute late in 5 of 5 answers.

## Czech

The pipeline handles Czech: on the Czech recording it covered 95.5% of the caption words with 3.2 s median drift, $0.15 for 64 minutes, no answers in a wrong script, and diacritics as often as in the captions. Colloquial forms stay as spoken ("kterej" 25 times against 25 in the captions). Weak spots:

- English loanwords come out phonetically: "fiat svět, jeden velkej skam" for "fiat world, one big scam".
- Lines are long, 105 characters on average and up to 527.
- One chunk looped on a paraphrased sentence until the output limit and needed a retry.
