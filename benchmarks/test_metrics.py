import json

import pytest

import metrics


def asr_vtt(timed_words: list[tuple[float, str]], per_cue: int = 4) -> str:
    """A WebVTT file shaped like YouTube's speech recognition captions: each cue repeats the previous line, then the
    new line with a time per word"""
    blocks = ['WEBVTT\nKind: captions\nLanguage: en']
    previous = ' '
    for i in range(0, len(timed_words), per_cue):
        cue = timed_words[i : i + per_cue]
        start, end = cue[0][0], cue[-1][0] + 0.5
        line = cue[0][1].replace('>>', '&gt;&gt;') + ''.join(
            f'<{stamp(t)}><c> {w.replace(">>", "&gt;&gt;")}</c>' for t, w in cue[1:]
        )
        blocks.append(f'{stamp(start)} --> {stamp(end)} align:start position:0%\n{previous}\n{line}')
        previous = ' '.join(w for _, w in cue).replace('>>', '&gt;&gt;')
    return '\n\n'.join(blocks) + '\n'


def stamp(t: float) -> str:
    return f'{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}'


def sentence(start: float, text: str, step: float = 0.5) -> list[tuple[float, str]]:
    return [(start + i * step, w) for i, w in enumerate(text.split())]


CAPTIONS = asr_vtt(
    sentence(0, 'so tell me how you started the company.')
    + sentence(4, '>> I was gonna be a teacher at first [music]')
    + sentence(10, 'but then I sold 66 cars in one summer.')
)


def test_reference_words_keep_word_times_speaker_markers_and_annotations():
    ref = metrics.reference_words(CAPTIONS)
    assert [r.word for r in ref[:3]] == ['so', 'tell', 'me']
    assert ref[1].time == 0.5
    assert [r.word for r in ref if r.speaker_change] == ['i']
    assert [r.word for r in ref if r.annotation] == ['music']
    assert 'music' not in [r.word for r in metrics.reference_words(CAPTIONS, drop_annotations=True)]


def test_normalize_spells_out_spoken_forms_and_drops_numbers():
    assert metrics.normalize(['i', 'was', 'gonna', 'sell', '66', 'sixty', 'six', 'cars']) == [
        'i',
        'was',
        'going',
        'to',
        'sell',
        'cars',
    ]


def transcript(*segments) -> metrics.Transcript:
    return metrics.transcript_from_segments(
        [{'start': start, 'speaker': speaker, 'text': text} | dict(*extra) for start, speaker, text, *extra in segments]
    )


def test_coverage_counts_missing_and_extra_words():
    ref = ['a', 'b', 'c', 'd']
    assert metrics.coverage(ref, ['a', 'b', 'x', 'd']) == {'recall': 0.75, 'insertions': 0.25}


def test_anchor_drift_measures_the_offset_of_matched_runs():
    ref = metrics.reference_words(CAPTIONS)
    tr = transcript(
        (2, 'Speaker 1', 'So tell me how you started the company?'),
        (6, 'Speaker 2', 'I was gonna be a teacher at first,'),
        (12, 'Speaker 2', 'but then I sold sixty six cars in one summer.'),
    )
    drift = metrics.anchor_drift(ref, tr.words, metrics.align_words(ref, tr.words), spacing=0)
    # Every word of a segment carries the segment's start, so the offset shrinks along the segment
    assert drift['anchors'] > 0
    assert drift['worst'] == (0, 2.0)


def test_longest_unmatched_run_and_long_losses_find_a_skipped_passage():
    ref = metrics.reference_words(CAPTIONS)
    tr = transcript((0, 'Speaker 1', 'so tell me how you started the company'), (10, 'Speaker 1', 'but then I sold'))
    alignment = metrics.align_words(ref, tr.words)
    run = metrics.longest_unmatched_run(ref, alignment)
    assert run['caption_words'] == 9
    assert run['at'] == 4
    # The five words missing at the end are a run too short to count as a lost passage
    assert metrics.lost_in_long_runs(alignment) == 9


def test_speaker_changes_match_markers_within_the_tolerance():
    ref = metrics.reference_words(CAPTIONS)
    tr = transcript(
        (0, 'Speaker 1', 'so tell me how you started the company'),
        (5, 'Speaker 2', 'I was gonna be a teacher at first'),
        (10, 'Speaker 1', 'but then'),
        (11, 'Speaker 2', 'I sold 66 cars in one summer'),
    )
    changes = metrics.speaker_changes(ref, tr.words, metrics.align_words(ref, tr.words))
    assert changes == {'changes': 3, 'markers': 1, 'precision': 0.333, 'recall': 1.0}


def test_word_timing_uses_each_word_own_time(tmp_path):
    scribe = {
        'audio_duration_secs': 3.0,
        'words': [
            {'type': 'word', 'text': 'so', 'start': 0.1, 'end': 0.3, 'speaker_id': 'speaker_0'},
            {'type': 'spacing', 'text': ' ', 'start': 0.3, 'end': 0.5},
            {'type': 'word', 'text': 'tell.', 'start': 0.6, 'end': 0.9, 'speaker_id': 'speaker_0'},
        ],
    }
    path = tmp_path / 'scribe.json'
    path.write_text(json.dumps(scribe))
    tr = metrics.load_transcript(path)
    assert tr.word_times and tr.duration == 3.0
    assert tr.segments == [{'start': 0.1, 'speaker': 'speaker_0', 'text': 'so tell.'}]
    ref = metrics.reference_words(CAPTIONS)
    timing = metrics.word_timing(ref, tr.words, metrics.align_words(ref, tr.words))
    assert timing['median'] == pytest.approx(0.1)
    assert timing['within_0.3s'] == 1.0


def test_label_agreement_redone_per_chunk_forgives_label_restarts():
    a = transcript(
        (0, 'Speaker 1', 'one two three four', {'chunk': 0}),
        (2, 'Speaker 2', 'five six seven eight', {'chunk': 0}),
        (4, 'Speaker 2', 'alpha beta gamma delta', {'chunk': 1}),
        (6, 'Speaker 1', 'epsilon zeta eta theta', {'chunk': 1}),
    )
    b = transcript(
        (0, 'host', 'one two three four'),
        (2, 'guest', 'five six seven eight'),
        (4, 'host', 'alpha beta gamma delta'),
        (6, 'guest', 'epsilon zeta eta theta'),
    )
    agreement = metrics.label_agreement(a, b)
    assert agreement['agreement_per_chunk_mapping'] == 1.0
    assert agreement['agreement_global_mapping'] == 0.5


def test_seam_loss_counts_caption_words_missing_next_to_the_seam():
    ref = metrics.reference_words(CAPTIONS)
    segments = [
        {'start': 0, 'speaker': 'Speaker 1', 'text': 'so tell me how you started the company', 'chunk': 0},
        {'start': 6, 'speaker': 'Speaker 1', 'text': 'a teacher at first music', 'chunk': 1},
    ]
    seam = metrics.seam_loss(ref, segments, 4.0, window=2.0)
    assert seam['lost_words'] == 'i was gonna'
    assert seam['duplicated_trigrams'] == 0


def test_cut_quality_reads_sentence_ends_speaker_changes_and_lead():
    captions = metrics.vtt_words(CAPTIONS)
    at_turn = metrics.cut_quality(captions, 3.8)
    assert (at_turn['before'], at_turn['after']) == ('company.', 'I')
    assert at_turn['sentence_end'] and at_turn['speaker_change'] and at_turn['boundary']
    assert at_turn['lead_seconds'] == 0.7
    mid = metrics.cut_quality(captions, 1.2)
    assert not mid['boundary']


def test_turn_purity_and_alternation():
    ref = metrics.reference_words(CAPTIONS)
    tr = transcript(
        (0, 'Speaker 1', 'so tell me how you started the company'),
        (4, 'Speaker 2', 'I was gonna be a teacher at first music but then I sold 66 cars in one summer'),
    )
    purity = metrics.turn_purity(ref, tr.words, metrics.align_words(ref, tr.words), min_words=5)
    assert purity == {'turns': 2, 'purity': 1.0, 'turns_below_0.8': 0, 'alternating': 1.0}


def test_diarization_changes_against_markers():
    turns = [(0.0, 3.9, 'S0'), (4.2, 9.0, 'S1'), (9.0, 9.5, 'S1'), (20.0, 25.0, 'S0')]
    result = metrics.diarization_changes(turns, [4.0, 30.0])
    assert result == {'changes': 2, 'markers': 2, 'markers_with_change': 0.5, 'changes_with_marker': 0.5}


def test_czech_measures():
    tokens = metrics.words('Todle je ten kterej to řekl, jo')
    assert metrics.diacritics_share(tokens) == round(1 / 7, 3)
    assert metrics.colloquial_counts(tokens)['kterej'] == 1
    assert metrics.colloquial_counts(tokens)['todle/tadle'] == 1
    assert metrics.number_style(['66', 'sixty', 'cars']) == {'digit_tokens': 1, 'number_words': 1}
