# Ideas and known gaps

Things found while building the service that are not done yet. Each entry says what is wrong or missing and what is known about fixing it. Remove an entry once it is done.

## Downloads

- **A pasted RSS feed downloads the whole feed.** yt-dlp's generic extractor claims RSS feeds and declares that it may return one item or several, so the collection guard lets it through. A fix: after extraction, reject a generic result that is a playlist with more than one entry, the same way collection extractors are rejected before download.
- **Spotify is not supported.** yt-dlp has no Spotify extractor and Spotify streams with DRM. Most podcasts on Spotify are also published through RSS; an Apple Podcasts episode link (`ApplePodcasts` extractor) or the episode's direct MP3 from the feed works instead.
- **Podcast transcripts can come free.** Many podcast feeds carry a `<podcast:transcript>` tag pointing to a transcript file. Reading it would give a transcript before paying a provider, the same way subtitles do for videos.

## Transcription

- **Czech numbers stay spelled out with Scribe.** `text2num` converts English number words to digits but has no Czech support, so Scribe's Czech transcripts read "šedesát šest procent". A converter would have to handle Czech declension; no maintained library was found.
- **The intro chapter as its own chunk is untested.** On the English test interview, the first chunk combined a montage of clips, a music bed, a sponsor message and a jingle, and produced the digit slips and early stops seen with Gemini. Cutting a short first chapter on its own would isolate that audio. See [transcription-chunking.md](transcription-chunking.md).
- **One Gemini failure passes every check:** an answer from an older prompt transcribed speech from two minutes later (5.6% coverage of the captions). A check that compares each chunk's text with the subtitles of the same time range, when subtitles exist, would catch it.
- **Untested providers:** Mistral Voxtral Mini Transcribe 3 (about $0.21 per hour, an EU company, Czech support unconfirmed) and Speechmatics Melia 1 (code-switching between languages mid-sentence, about $0.24 per hour batch). See [research.md](research.md).
- **Speaker names instead of numbers.** Scribe keeps one label per speaker for the whole file, but the labels are numbers. The uploader or channel name, names in the title and description, and each speaker's self-introduction could map the numbers to names. Local diarization with pyannote needs a GPU to be fast enough; see [transcription-models.md](transcription-models.md).
- **Moving transcription into its own service**, so other projects can use it: an API and MCP server behind authentication, running as cloud functions, with this service as one client.

## Tools

- **The CLI cannot read MCP resources.** `mcp-to-cli-kit` supports tools only, so the download log and the metadata resources are out of reach through `video-download`; transcripts are still downloadable files. A `get_log` tool would cover the log.
