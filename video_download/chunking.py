"""Where a long recording is cut into the chunks that are transcribed one request each.

Recordings under CHUNKED_FROM_SECONDS are never cut. Longer ones are cut at their chapters when info.json has them:
consecutive chapters are merged up to the target length, and a group longer than the maximum is split. Without chapters
the recording is split into chunks of at most the target length. Every cut lies inside a pause that voice activity
detection found, so no word is split and the chunks need no overlap: the transcript is the chunks' segments one after
another, each shifted by its chunk's start.

Chapter times are typed in by hand in whole seconds, so a chapter cut moves to the longest pause near the chapter time,
and snap_distance records how far it moved.
"""

import bisect
import difflib
import math
import re
from dataclasses import dataclass, field

from video_download.subtitles import CaptionWord

CHUNKED_FROM_SECONDS = 7 * 60
# On a 91-minute interview, Gemini 3.5 Flash-Lite stopped early, skipped passages or made up speech in 12 of 22 answers
# for chunks of 7 to 10 minutes; with chunks of 2 to 7 minutes 5 of 30 answers failed the checks
DEFAULT_TARGET_SECONDS = 5 * 60


@dataclass(frozen=True)
class Lengths:
    """How long the chunks are: merged up to target, split above maximum, and never shorter than minimum"""

    target: float = DEFAULT_TARGET_SECONDS

    @property
    def maximum(self) -> float:
        return self.target * 1.4

    @property
    def minimum(self) -> float:
        return self.target * 0.4


# A pause this long is a safe cut; shorter gaps between words are only taken when there is nothing better
MIN_PAUSE_SECONDS = 0.3
# Where to look for the pause of a chapter cut, first close to the chapter time, then wider
SNAP_SECONDS = 15
SNAP_WIDE_SECONDS = 45
# Where to look for the pause of a cut without a chapter, around the time that would make the chunks equally long
SPLIT_SECONDS = 60
SPLIT_WIDE_SECONDS = 120

# How a chunk's start was chosen
START = 'start'
CHAPTER = 'chapter'
PAUSE = 'pause'
# No pause was found, or there was no speech detection at all: the cut may split a word
HARD = 'hard'


@dataclass
class Cut:
    time: float
    method: str
    # Seconds from the chapter time, or from the time that would make the chunks equally long
    snap_distance: float | None = None
    # Length of the pause the cut lies in
    pause: float | None = None
    # What the subtitles show at the cut: sentence, speaker, sentence+speaker, mid-sentence; None without subtitles
    boundary: str | None = None


@dataclass
class ChunkPlan:
    index: int
    start: float
    end: float
    cut: Cut
    # Titles of the chapters in the chunk, the one it starts in first
    chapters: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Plan:
    chunks: list[ChunkPlan]
    # The chapters of info.json, (start, title), for the headings of the transcript
    chapters: list[tuple[float, str]] = field(default_factory=list)


def pauses_between(speech: list[tuple[float, float]] | None, duration: float) -> list[tuple[float, float]] | None:
    """The gaps between the speech regions, None when speech detection did not run"""
    if speech is None:
        return None
    pauses = []
    position = 0.0
    for start, end in speech:
        if start > position:
            pauses.append((position, start))
        position = max(position, end)
    if duration > position:
        pauses.append((position, duration))
    return pauses


def longest_pause(pauses, lo: float, hi: float, target: float, min_length: float) -> tuple[float, float] | None:
    """The middle and length of the longest pause inside [lo, hi], of at least min_length; ties go to the one nearer
    the target"""
    best = None
    for start, end in pauses:
        start, end = max(start, lo), min(end, hi)
        length = end - start
        if length <= 0 or length < min_length:
            continue
        middle = (start + end) / 2
        key = (round(length, 3), -abs(middle - target))
        if best is None or key > best[0]:
            best = (key, middle, length)
    return (best[1], best[2]) if best else None


# Gemini tends to drop the words around a cut, least so at the end of a sentence or of a speaker's turn. The subtitles
# show where those are; each property of a pause adds or takes points, the pause with the most wins.
SENTENCE_POINTS = 2.0
# A capitalized next word, for subtitles without punctuation; "I" is capitalized anywhere
CAPITAL_POINTS = 1.0
SPEAKER_POINTS = 2.0
# Per second of pause, up to MAX_PAUSE_POINTS_SECONDS
PAUSE_POINTS = 1.0
MAX_PAUSE_POINTS_SECONDS = 2.0
# The cut is less than MIN_LEAD_SECONDS before the next word starts, or a word starts inside the pause
CLOSE_WORD_POINTS = -2.0
MIN_LEAD_SECONDS = 0.5
# Per second away from the chapter time or the time that makes the chunks equally long
DISTANCE_POINTS_PER_SECOND = -1 / 15
# Subtitle and speech detection times differ by this much
CAPTION_SLACK_SECONDS = 0.3
SENTENCE_END_RE = re.compile(r'[.?!…]["\'”’)\]]*$')
# Subtitles whose words mostly start outside the detected speech belong to another edit of the video
MIN_CAPTIONS_IN_SPEECH = 0.7


class Captions:
    """The words of the job's subtitles, to tell where sentences and speaker turns end"""

    def __init__(self, words: list[CaptionWord]):
        self.words = sorted(words, key=lambda word: word.time)
        self.times = [word.time for word in self.words]

    def aligned_with(self, speech: list[tuple[float, float]]) -> bool:
        if not self.words:
            return False
        starts = [start for start, _ in speech]
        inside = 0
        for time in self.times:
            index = bisect.bisect_right(starts, time + CAPTION_SLACK_SECONDS) - 1
            inside += index >= 0 and time <= speech[index][1] + CAPTION_SLACK_SECONDS
        return inside / len(self.times) >= MIN_CAPTIONS_IN_SPEECH

    def boundary(self, start: float, end: float) -> tuple[float, str]:
        """Points of a cut in the middle of the pause [start, end), and what the subtitles show there"""
        middle = (start + end) / 2
        following = bisect.bisect_left(self.times, middle - CAPTION_SLACK_SECONDS)
        if following >= len(self.words):
            return 0.0, 'end'
        points = 0.0
        labels = []
        previous = self.words[following - 1] if following else None
        word = self.words[following]
        if previous is None or SENTENCE_END_RE.search(previous.text):
            points += SENTENCE_POINTS
            labels.append('sentence')
        elif word.text[:1].isupper() and word.text != 'I' and not word.text.startswith("I'"):
            points += CAPITAL_POINTS
            labels.append('capital')
        if word.speaker_change:
            points += SPEAKER_POINTS
            labels.append('speaker')
        inside = bisect.bisect_left(self.times, start + CAPTION_SLACK_SECONDS) < bisect.bisect_right(
            self.times, end - CAPTION_SLACK_SECONDS
        )
        if word.time - middle < MIN_LEAD_SECONDS or inside:
            points += CLOSE_WORD_POINTS
        return points, '+'.join(labels) or 'mid-sentence'


def best_pause(pauses, lo, hi, target, min_length, captions: Captions | None):
    """The middle, length and subtitle boundary of the best pause inside [lo, hi] of at least min_length: the longest
    without subtitles, the one with the most points with them"""
    if captions is None:
        found = longest_pause(pauses, lo, hi, target, min_length)
        return (*found, None) if found else None
    best = None
    for start, end in pauses:
        start, end = max(start, lo), min(end, hi)
        length = end - start
        if length <= 0 or length < min_length:
            continue
        middle = (start + end) / 2
        points, boundary = captions.boundary(start, end)
        points += PAUSE_POINTS * min(length, MAX_PAUSE_POINTS_SECONDS)
        points += DISTANCE_POINTS_PER_SECOND * abs(middle - target)
        if best is None or points > best[0]:
            best = (points, middle, length, boundary)
    return best[1:] if best else None


def find_cut(pauses, target: float, windows: list[tuple[float, float]], method: str, captions=None) -> Cut:
    """A cut in the best pause of the first window that has one: of MIN_PAUSE_SECONDS in any window, then of any
    length in the widest; a hard cut at the target without one"""
    if pauses:
        stages = [(lo, hi, MIN_PAUSE_SECONDS) for lo, hi in windows] + [(*windows[-1], 0.05)]
        for lo, hi, min_length in stages:
            if hi < lo:
                continue
            found = best_pause(pauses, lo, hi, target, min_length, captions)
            if found:
                time, length, boundary = found
                return Cut(round(time, 2), method, round(time - target, 2), round(length, 2), boundary)
    return Cut(round(target, 2), HARD, 0.0, None)


def split_range(
    pauses, start: float, end: float, first_cut: Cut, lengths: Lengths, captions: Captions | None = None
) -> list[tuple[float, float, Cut]]:
    """[start, end) cut into chunks of at most the target length, about equally long, each cut in a pause"""
    pieces = []
    count = math.ceil((end - start) / lengths.target - 1e-9)
    position, cut = start, first_cut
    for remaining in range(count, 1, -1):
        target = position + (end - position) / remaining
        lo_limit = position + lengths.minimum
        hi_limit = min(position + lengths.maximum, end - lengths.minimum)
        windows = [
            (max(lo_limit, target - width), min(hi_limit, target + width))
            for width in (SPLIT_SECONDS, SPLIT_WIDE_SECONDS)
        ]
        next_cut = find_cut(pauses, target, windows, PAUSE, captions)
        pieces.append((position, next_cut.time, cut))
        position, cut = next_cut.time, next_cut
    pieces.append((position, end, cut))
    return pieces


def snap_chapters(
    pauses, chapters: list[tuple[float, str]], duration: float, captions: Captions | None = None
) -> list[tuple[Cut, str]]:
    """Each chapter after the first with its time moved into a pause nearby; a chapter that would not come after the
    previous one is left out"""
    snapped = []
    previous = 0.0
    for time, title in sorted(chapters):
        if time <= 0 or time >= duration:
            continue
        windows = [(time - width, time + width) for width in (SNAP_SECONDS, SNAP_WIDE_SECONDS)]
        cut = find_cut(pauses, time, windows, CHAPTER, captions)
        if cut.time <= previous or cut.time >= duration:
            continue
        snapped.append((cut, title))
        previous = cut.time
    return snapped


def plan_chunks(
    duration: float,
    speech: list[tuple[float, float]] | None,
    chapters: list[tuple[float, str]] | None = None,
    lengths: Lengths | None = None,
    captions: list[CaptionWord] | None = None,
) -> Plan:
    """speech: the speech regions of the audio, None when detection failed; chapters: (start, title) from info.json;
    captions: the words of the job's subtitles, ignored when they do not line up with the speech"""
    lengths = lengths or Lengths()
    chapters = [(float(time), title) for time, title in chapters or []]
    if duration < CHUNKED_FROM_SECONDS:
        return Plan([ChunkPlan(0, 0.0, duration, Cut(0.0, START), [title for _, title in chapters])], chapters)
    pauses = pauses_between(speech, duration)
    words = Captions(captions or [])
    words = words if speech and words.aligned_with(speech) else None
    snapped = snap_chapters(pauses, chapters, duration, words)
    # The headings stay at the chapter times: a cut moved away from a chapter only because there was no pause there
    marks = sorted(chapters)

    # Greedy groups of whole chapters up to the target length; a group under the minimum takes the next chapter anyway
    boundaries = [cut for cut, _ in snapped] + [Cut(duration, 'end')]
    groups: list[tuple[float, float, Cut]] = []
    group_start, group_cut, group_end = 0.0, Cut(0.0, START), None
    for boundary in boundaries:
        if (
            group_end is not None
            and boundary.time - group_start > lengths.target
            and group_end.time - group_start >= lengths.minimum
        ):
            groups.append((group_start, group_end.time, group_cut))
            group_start, group_cut = group_end.time, group_end
        group_end = boundary
    groups.append((group_start, duration, group_cut))
    # A short last group joins the one before when they fit together
    last, before = groups[-1], groups[-2] if len(groups) > 1 else None
    if before and last[1] - last[0] < lengths.minimum and last[1] - before[0] <= lengths.maximum:
        groups[-2:] = [(before[0], last[1], before[2])]

    pieces = []
    for start, end, cut in groups:
        pieces += (
            split_range(pauses, start, end, cut, lengths, words)
            if end - start > lengths.maximum
            else [(start, end, cut)]
        )

    chunks = []
    for index, (start, end, cut) in enumerate(pieces):
        current = [title for time, title in marks if time <= start][-1:]
        inside = [title for time, title in marks if start < time < end]
        chunks.append(ChunkPlan(index, round(start, 2), round(end, 2), cut, current + inside))
    return Plan(chunks, marks)


# --- Measures of a chunk's transcript, for its validation


def speech_seconds(speech: list[tuple[float, float]], start: float, end: float) -> float:
    """How much of [start, end) is speech"""
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in speech)


def largest_blind_speech(starts: list[float], speech: list[tuple[float, float]], start: float, end: float) -> float:
    """The most speech between two consecutive segment starts, or before the first or after the last: a transcript that
    skipped a passage has a long stretch of speech without any segment"""
    edges = [start, *sorted(t for t in starts if start <= t <= end), end]
    return max(speech_seconds(speech, a, b) for a, b in zip(edges, edges[1:], strict=False))


WORD_RE = re.compile(r'\w+')


def words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower().replace("'", '').replace('’', ''))


def recall(reference: list[str], transcript: list[str]) -> float:
    """The share of the reference words that the transcript has, in the same order"""
    if not reference:
        return 1.0
    matcher = difflib.SequenceMatcher(None, reference, transcript, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(reference)


def most_repeated_line(texts) -> int:
    """How often the most frequent line of four or more words occurs; a model caught in a loop repeats one line"""
    counts: dict[str, int] = {}
    for text in texts:
        line = words(text)
        if len(line) >= 4:
            key = ' '.join(line)
            counts[key] = counts.get(key, 0) + 1
    return max(counts.values(), default=0)


# How many timestamps of a chunk's answer may be out of line before the answer counts as broken instead of repaired
MAX_REPAIRED_SHARE = 0.1
MIN_REPAIRED_ALLOWED = 3


def repair_times(starts: list[int], duration: float) -> tuple[list[int], int] | None:
    """The segment starts of a chunk's answer with the few that are out of line moved between their neighbours, and
    how many moved; None when too many are out of line to tell which ones are right.

    Gemini writes an occasional timestamp wrong in a way that stands out: an hour digit in a chunk of minutes
    ([01:01:01] for [00:01:01]), or a few lines minutes ahead of the rest. The longest run of starts that never goes
    back is taken as right.
    """
    if duration < 3600:
        starts = [start % 3600 for start in starts]
    count = len(starts)
    if not count:
        return [], 0
    # Longest non-decreasing subsequence within the chunk, O(n²) for the few hundred lines of a chunk
    valid = [0 <= start <= duration for start in starts]
    best = [1 if valid[i] else 0 for i in range(count)]
    previous = [-1] * count
    for i in range(count):
        if not valid[i]:
            continue
        for j in range(i):
            if valid[j] and starts[j] <= starts[i] and best[j] + 1 > best[i]:
                best[i], previous[i] = best[j] + 1, j
    end = max(range(count), key=lambda i: best[i])
    kept = set()
    while end >= 0 and best[end]:
        kept.add(end)
        end = previous[end]
    moved = count - len(kept)
    if not kept or moved > max(MIN_REPAIRED_ALLOWED, count * MAX_REPAIRED_SHARE):
        return None
    repaired = list(starts)
    i = 0
    while i < count:
        if i in kept:
            i += 1
            continue
        j = i
        while j < count and j not in kept:
            j += 1
        low = repaired[i - 1] if i > 0 else 0
        high = repaired[j] if j < count else max(low, min(duration, low))
        for k in range(i, j):
            repaired[k] = low + (high - low) * (k - i + 1) // (j - i + 1)
        i = j
    return repaired, moved
