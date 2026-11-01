# Research notes

Findings from desk research done while building the service, without our own measurements. Dates matter: the speech and model landscape changes monthly. Each section says when it was checked.

## Speech-to-text alternatives (checked 2026-11-01)

| Option | Est. $ per hour of audio | Czech | Evidence | EU |
|---|---|---|---|---|
| Gemini 3.5 Flash-Lite, AI Studio (in use) | ~0.15, measured | works, see transcription-models.md | Artificial Analysis WER 3.4% for 3.1 Flash-Lite | no residency guarantee |
| Gemini 3.5 Transcribe (AI Studio or Vertex) | ~0.30 | 85+ languages, no Czech figure | AA-WER 2.6%, FLEURS 5.0% (Google) | Vertex `eu` multi-region |
| ElevenLabs Scribe v2 | ~0.22 | FLEURS 3.1% (vendor) | AA-WER 2.2% | residency claimed, unverified |
| Mistral Voxtral Mini Transcribe 3 | ~0.21 | not verified | AA-WER 2.2% | EU company |
| Groq Whisper v3 Turbo | ~0.04 | yes | AA-WER 4.6% | US only |
| Google Chirp 3 | 0.96 standard | preview only, no diarization | AA-WER 4.3% | `us` and `eu` multi-regions |
| Amazon Transcribe | ~0.36 | cs-CZ supported | AA-WER 4.1%, no Czech data | EU regions |

- Artificial Analysis WER is measured mostly on short English clips. It says little about Czech or long interviews.
- On Vertex AI only the `eu` multi-region endpoint guarantees EU data residency; Google states that single-region endpoints do not. Non-global endpoints cost about 10% more.
- Amazon Bedrock has refused audio input for Voxtral since about 2026-10-21, and Nova has no transcription path in EU regions.
- OpenRouter charges the same token price as Google for Gemini audio and takes audio only as base64. EU routing is not guaranteed.

## Cut-point detectors (checked 2026-11-01)

The service is meant to run on modest hardware: a few CPU cores, no GPU, possibly no AVX2. That rules out heavy models for anything in the request path.

- **Other VADs** (WebRTC, TEN, FireRedVAD, NVIDIA MarbleNet, pyannote, SpeechBrain) bring no gain over Silero, need PyTorch, or carry license limits. TEN VAD's terms forbid use that competes with Agora.
- **Turn detectors:** Pipecat Smart Turn v3.2 (BSD, 8 MB ONNX) can score candidate pauses offline but is untested on Czech. LiveKit's turn detector may only run inside LiveKit Agents.
- **Punctuation restoration** models that cover Czech are about 1 GB. YouTube's captions already carry punctuation and speaker markers, which made them the chosen signal.
- **sherpa-onnx** (Apache-2.0) bundles VAD, speaker diarization and punctuation, with Python wheels for 3.14, but its punctuation models do not cover Czech.

## How the Gemini app handles YouTube links (checked 2026-11-01)

Google has not documented it. A leaked system prompt of YouTube's "Ask" assistant gives the model a timestamped transcript, and users have reported "no transcript, no summary" since 2024, so captions look like the default source. Google announced "agentic video" on 2026-09-01: it searches the transcript first and loads frames and audio only where needed. It is live in the API and was "rolling out soon" to the app. The Gemini API processes a YouTube URL natively, sampling frames once per second plus the audio.

## Using a YouTube account with yt-dlp (checked 2026-10-31)

- Cookies are the only way to log in; yt-dlp removed OAuth in 2024. Export them from a private browser window that you then close, because YouTube replaces the cookies of a session that stays open.
- yt-dlp's wiki warns that YouTube can ban an account used with yt-dlp. Reported bans block YouTube playback for weeks to months; YouTube's terms allow terminating the whole Google account.
- A fresh account in the EU may need age verification (ID, card or selfie) before age-restricted videos play, so a throwaway account does not fix age gates.
- Logged-in cookies make yt-dlp use different YouTube player clients, which can offer fewer formats. The service sends YouTube cookies only on a retry after a login check.

## MCP long-running tasks (checked 2026-10-31)

- MCP 2025-11-25 added experimental tasks to the core protocol. Spec 2026-07-28 moved them into the official extension `io.modelcontextprotocol/tasks` (SEP-2663) with a wire-incompatible design: the server decides per `tools/call` whether to answer with a task, and the client polls `tasks/get`.
- Spec 2026-07-28 is stateless: no `initialize` handshake, no session id, capabilities on every request, and no SSE resumption.
- In October 2026 only the Claude Code CLI, claude.ai and MCPJam supported tasks. Claude Code supports them in the terminal CLI only, in the main conversation, on protocol 2026-07-28.
- In Python, only FastMCP 4.x implements the tasks extension; the official `mcp` SDK v2 had no tasks runtime yet.
- claude.ai connectors call MCP servers from Anthropic's cloud, so they cannot reach a Tailscale-only server.
