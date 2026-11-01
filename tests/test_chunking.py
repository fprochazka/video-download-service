import pytest

from video_download import chunking
from video_download.chunking import Lengths


def speech_with_pauses(duration: float, pauses: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Speech everywhere except in these (start, end) pauses"""
    regions, position = [], 0.0
    for start, end in sorted(pauses):
        regions.append((position, start))
        position = end
    regions.append((position, duration))
    return regions


def spans(plan):
    return [(c.start, c.end, c.cut.method) for c in plan.chunks]


@pytest.mark.parametrize('duration', [60, 6 * 60 + 59])
def test_a_recording_under_7_minutes_is_one_chunk(duration):
    chapters = [(0, 'Intro'), (300, 'Engines')]

    plan = chunking.plan_chunks(duration, speech_with_pauses(duration, [(299, 301)]), chapters)

    assert spans(plan) == [(0.0, duration, 'start')]
    assert plan.chunks[0].chapters == ['Intro', 'Engines']


def test_chapters_are_merged_up_to_the_target_and_cut_in_the_longest_pause_near_them():
    # Two chapters of 2 minutes fit together; a short pause right at 240 s and a longer one 7 s later
    chapters = [(0, 'One'), (120, 'Two'), (240, 'Three'), (420, 'Four'), (600, 'Five'), (780, 'Six')]
    pauses = [(119.5, 120.5), (239.8, 240.2), (247.0, 248.0), (419.0, 421.0), (598.0, 599.0), (779.5, 780.5)]
    speech = speech_with_pauses(1000, pauses)

    plan = chunking.plan_chunks(1000, speech, chapters)

    assert spans(plan) == [
        (0.0, 247.5, 'start'),
        (247.5, 420.0, 'chapter'),
        (420.0, 598.5, 'chapter'),
        (598.5, 780.0, 'chapter'),
        (780.0, 1000.0, 'chapter'),
    ]
    second = plan.chunks[1]
    assert (second.cut.snap_distance, second.cut.pause) == (7.5, 1.0)
    # A chunk lists the chapter it starts in, then those that start inside it
    assert second.chapters == ['Three']
    assert plan.chunks[3].chapters == ['Four', 'Five']
    # The headings stay at the chapter times
    assert plan.chapters == chapters


def test_a_chapter_without_a_pause_nearby_looks_wider_and_then_cuts_hard():
    chapters = [(0, 'One'), (300, 'Two'), (600, 'Three')]
    # A pause 30 s after the second chapter, none anywhere near the third
    speech = speech_with_pauses(1000, [(329.0, 330.0)])

    plan = chunking.plan_chunks(1000, speech, chapters)

    assert spans(plan) == [(0.0, 329.5, 'start'), (329.5, 600.0, 'chapter'), (600.0, 1000.0, 'hard')]
    assert plan.chunks[1].cut.snap_distance == 29.5


def test_a_long_chapter_is_split_at_pauses():
    # A one-minute intro is too short to be a chunk of its own
    chapters = [(0, 'Intro'), (60, 'Everything')]
    pauses = [(59, 61)] + [(t, t + 1) for t in range(150, 1300, 97)]

    plan = chunking.plan_chunks(1300, speech_with_pauses(1300, pauses), chapters)

    assert [c.cut.method for c in plan.chunks] == ['start', 'pause', 'pause', 'pause', 'pause']
    assert all(c.duration <= Lengths().maximum for c in plan.chunks)
    assert plan.chunks[0].chapters == ['Intro', 'Everything']
    assert [c.chapters for c in plan.chunks[1:]] == [['Everything']] * 4


def test_without_chapters_the_chunks_are_about_equal_and_cut_in_pauses():
    pauses = [(t, t + 0.8) for t in range(37, 1800, 41)]

    plan = chunking.plan_chunks(1800, speech_with_pauses(1800, pauses), [])

    assert len(plan.chunks) == 6
    assert all(c.cut.method in ('start', 'pause') for c in plan.chunks)
    assert all(Lengths().minimum <= c.duration <= Lengths().maximum for c in plan.chunks)
    # Every cut is the middle of a pause
    assert all(any(s < c.start < e for s, e in pauses) for c in plan.chunks[1:])
    assert plan.chunks[-1].end == 1800


def test_without_speech_detection_the_cuts_are_hard_at_equal_lengths():
    plan = chunking.plan_chunks(1200, None, [])

    assert spans(plan) == [
        (0.0, 300.0, 'start'),
        (300.0, 600.0, 'hard'),
        (600.0, 900.0, 'hard'),
        (900.0, 1200.0, 'hard'),
    ]


def test_a_short_last_chapter_joins_the_chunk_before():
    chapters = [(0, 'One'), (280, 'Two'), (560, 'Three'), (900, 'Short end')]
    pauses = [(279.5, 280.5), (559.5, 560.5), (899.5, 900.5)]

    plan = chunking.plan_chunks(960, speech_with_pauses(960, pauses), chapters)

    assert spans(plan) == [(0.0, 280.0, 'start'), (280.0, 560.0, 'chapter'), (560.0, 960.0, 'chapter')]


def test_the_target_length_is_configurable():
    plan = chunking.plan_chunks(1200, None, [], Lengths(600))

    assert spans(plan) == [(0.0, 600.0, 'start'), (600.0, 1200.0, 'hard')]


def test_pauses_are_the_gaps_between_speech():
    assert chunking.pauses_between([(1, 2), (3, 5)], 6) == [(0.0, 1), (2, 3), (5, 6)]
    assert chunking.pauses_between(None, 6) is None


def test_blind_speech_is_the_most_speech_between_two_segment_starts():
    speech = [(0, 50), (60, 100)]

    assert chunking.largest_blind_speech([0, 10, 20, 90], speech, 0, 100) == 60
    assert chunking.largest_blind_speech([], speech, 0, 100) == 90
    assert chunking.speech_seconds(speech, 40, 70) == 20


def test_recall_counts_the_reference_words_the_transcript_has_in_order():
    reference = chunking.words("Hello, it's a nice day today")

    assert reference == ['hello', 'its', 'a', 'nice', 'day', 'today']
    assert chunking.recall(reference, chunking.words('hello its a day today')) == pytest.approx(5 / 6)
    assert chunking.recall([], ['x']) == 1.0


def test_most_repeated_line_ignores_short_lines():
    texts = ['Yeah.', 'Yeah.', 'Yeah.', 'We went to the shop.', 'We went to the shop!', 'Other words are here.']

    assert chunking.most_repeated_line(texts) == 2


@pytest.mark.parametrize(
    ('starts', 'duration', 'repaired'),
    [
        ([0, 5, 10], 60, ([0, 5, 10], 0)),
        # An hour digit in a chunk of minutes
        ([0, 2, 3661, 3673], 120, ([0, 2, 61, 73], 0)),
        # A few lines minutes ahead of the rest
        ([122, 123, 127, 17, 18, 24, 30], 123, ([4, 8, 12, 17, 18, 24, 30], 3)),
        # One line far ahead between good ones
        ([0, 700, 10, 20, 30, 40, 50, 60, 70, 80, 90], 100, ([0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90], 1)),
        # Everything off: nothing to repair from
        ([180, 420, 1020, 1080, 1440], 35, None),
        ([1200, 1260], 300, None),
        ([], 35, ([], 0)),
    ],
)
def test_out_of_line_timestamps_are_repaired(starts, duration, repaired):
    assert chunking.repair_times(starts, duration) == repaired
