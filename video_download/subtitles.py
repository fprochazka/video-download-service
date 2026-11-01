"""Which subtitle tracks a job downloads: the video's own language and English, nothing of the machine translations"""

import re
from dataclasses import dataclass

ENGLISH = 'en'

# YouTube's speech recognition track in the video's language, next to it are ~100 machine translations of it
ORIGINAL_AUTO_SUFFIX = '-orig'

# Not subtitles, YouTube lists the replay of a live chat as a subtitle track
IGNORED_TRACKS = frozenset({'live_chat'})

# What yt-dlp writes as subtitles; with subtitlesformat vtt/best the rest only appears when a site has no vtt
SUBTITLE_EXTENSIONS = frozenset({'vtt', 'srt', 'ass', 'ssa', 'ttml', 'dfxp', 'srv1', 'srv2', 'srv3', 'json3', 'lrc'})


def base_language(code: str) -> str:
    return code.split('-')[0].lower()


def original_language(info: dict) -> str | None:
    """The language spoken in the video, from yt-dlp's language field or else from YouTube's -orig auto track"""
    if info.get('language'):
        return base_language(info['language'])
    for key in info.get('automatic_captions') or {}:
        if key.endswith(ORIGINAL_AUTO_SUFFIX):
            return base_language(key.removesuffix(ORIGINAL_AUTO_SUFFIX))
    return None


def select_subtitle_tracks(info: dict) -> dict[str, bool]:
    """Track keys to download, mapped to whether the track is automatic captions.

    A manual track of a language wins over its automatic captions: all manual tracks of the language are taken
    (en, en-US, en-GB), automatic captions only when there is no manual track, and then only one track,
    the speech recognition one (<lang>-orig) if there is one.
    """
    manual = [key for key in info.get('subtitles') or {} if key not in IGNORED_TRACKS]
    auto = [key for key in info.get('automatic_captions') or {} if key not in IGNORED_TRACKS]

    languages = [original_language(info), ENGLISH]
    selected: dict[str, bool] = {}
    for language in dict.fromkeys(lang for lang in languages if lang):
        manual_tracks = [key for key in manual if base_language(key) == language]
        if manual_tracks:
            selected.update(dict.fromkeys(manual_tracks, False))
            continue
        for key in (language + ORIGINAL_AUTO_SUFFIX, language):
            if key in auto:
                selected[key] = True
                break
    return selected


def subtitleslangs_option(tracks: dict[str, bool]) -> list[str]:
    """yt-dlp matches subtitleslangs as regexes, the keys have to match only themselves"""
    return [re.escape(key) for key in tracks]


def subtitle_track_of(filename: str, tracks: dict[str, bool]) -> str | None:
    """The track key of a subtitle file yt-dlp wrote, named <media name without ext>.<track key>.<subtitle ext>"""
    stem, dot, ext = filename.rpartition('.')
    if not dot or ext not in SUBTITLE_EXTENSIONS:
        return None
    for key in tracks:
        if stem.endswith('.' + key):
            return key
    return None


VTT_CUE_TIME_RE = re.compile(r'^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->')
VTT_TAG_RE = re.compile(r'<[^>]*>')


def vtt_cues(content: str) -> list[tuple[float, str]]:
    """(start in seconds, text) of each cue of a WebVTT file, without styling tags.

    YouTube's automatic captions repeat the previous line at the top of each cue as the text rolls up, a line equal to
    the last one kept is dropped.
    """
    cues = []
    last_line = None
    start = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if match := VTT_CUE_TIME_RE.match(line):
            hours, minutes, seconds, millis = match.groups()
            start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
            continue
        if not line:
            # A blank line ends the cue; cue identifiers and NOTE blocks outside a cue are skipped
            start = None
            continue
        if start is None:
            continue
        text = ' '.join(VTT_TAG_RE.sub('', line).replace('&nbsp;', ' ').split())
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        if text and text != last_line:
            cues.append((start, text))
            last_line = text
    return cues


VTT_CUE_RANGE_RE = re.compile(
    r'^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})'
)
# YouTube's speech recognition captions time each word: word<00:00:01.320><c> word</c>
VTT_WORD_TIME_RE = re.compile(r'<(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})>')
# A new speaker: >> in YouTube's captions, a leading dash in many manual subtitles
SPEAKER_MARK_RE = re.compile(r'^(?:>>|[-–—]\s)\s*')


@dataclass(frozen=True)
class CaptionWord:
    time: float
    text: str
    # The word starts a new speaker's turn
    speaker_change: bool = False


def vtt_seconds(hours, minutes, seconds, millis) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def unescape(text: str) -> str:
    text = ' '.join(VTT_TAG_RE.sub('', text).replace('&nbsp;', ' ').split())
    return text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


def vtt_words(content: str) -> list[CaptionWord]:
    """Each word of a WebVTT file with the time it starts, sorted by it.

    Words of YouTube's speech recognition captions carry their own time, and only their lines with word times count:
    the other lines repeat the previous ones as the text rolls up. Words of other subtitles are spread evenly over
    their cue.
    """
    timed = '<c>' in content
    words: list[CaptionWord] = []
    start = end = None
    last_line = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if match := VTT_CUE_RANGE_RE.match(line):
            start, end = vtt_seconds(*match.groups()[:4]), vtt_seconds(*match.groups()[4:])
            continue
        if not raw_line:
            start = None
            continue
        # YouTube starts a cue with a line of one space where the rolled-up line would be
        if not line or start is None or (timed and '<c>' not in line):
            continue
        parts = VTT_WORD_TIME_RE.split(line)
        # parts: text, then (hours, minutes, seconds, millis, text) for each timed word
        pieces = [(start, parts[0])] + [(vtt_seconds(*parts[i : i + 4]), parts[i + 4]) for i in range(1, len(parts), 5)]
        texts = [(time, unescape(text)) for time, text in pieces]
        plain = ' '.join(text for _, text in texts)
        if not timed:
            if plain == last_line:
                continue
            last_line = plain
        speaker = bool(SPEAKER_MARK_RE.match(plain))
        tokens = []
        for time, text in texts:
            text = SPEAKER_MARK_RE.sub('', text) if not tokens else text
            tokens += [(time, token) for token in text.split()]
        if not timed and tokens:
            step = max(0.0, end - start) / len(tokens)
            tokens = [(start + i * step, token) for i, (_, token) in enumerate(tokens)]
        words += [CaptionWord(time, token, speaker and i == 0) for i, (time, token) in enumerate(tokens)]
    return sorted(words, key=lambda word: word.time)
