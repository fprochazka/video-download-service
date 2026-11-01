import pytest

from video_download import subtitles
from video_download.subtitles import CaptionWord, vtt_words

# A part of the ~100 languages YouTube offers as machine translations of the speech recognition track
TRANSLATION_LANGUAGES = ['ab', 'af', 'ar', 'cs', 'de', 'en', 'es', 'fr', 'hu', 'ja', 'pl', 'sk', 'uk', 'zh-Hans']


def tracks(*keys: str) -> dict:
    return {key: [{'ext': 'json3', 'url': f'https://example.com/{key}.json3'}, {'ext': 'vtt'}] for key in keys}


def youtube_auto_captions(original: str) -> dict:
    return tracks(f'{original}-orig', *TRANSLATION_LANGUAGES)


def test_english_video_with_manual_tracks_takes_them_over_auto_captions():
    info = {
        'language': 'en',
        'subtitles': tracks('en', 'en-GB', 'de', 'live_chat'),
        'automatic_captions': youtube_auto_captions('en'),
    }

    assert subtitles.select_subtitle_tracks(info) == {'en': False, 'en-GB': False}


def test_czech_video_with_auto_captions_only_takes_the_speech_recognition_and_english_translation():
    info = {'language': None, 'subtitles': {}, 'automatic_captions': youtube_auto_captions('cs')}

    assert subtitles.select_subtitle_tracks(info) == {'cs-orig': True, 'en': True}


def test_language_field_wins_over_the_orig_track():
    info = {'language': 'sk-SK', 'subtitles': tracks('sk'), 'automatic_captions': youtube_auto_captions('cs')}

    assert subtitles.select_subtitle_tracks(info) == {'sk': False, 'en': True}


def test_manual_original_and_auto_english():
    info = {'language': 'de', 'subtitles': tracks('de-DE'), 'automatic_captions': youtube_auto_captions('de')}

    assert subtitles.select_subtitle_tracks(info) == {'de-DE': False, 'en': True}


def test_without_language_information_only_english_is_taken():
    info = {'subtitles': tracks('en-US', 'fr'), 'automatic_captions': tracks('fr', 'en')}

    assert subtitles.select_subtitle_tracks(info) == {'en-US': False}


def test_auto_track_without_orig_suffix_is_used_for_the_original_language():
    info = {'language': 'pl', 'automatic_captions': tracks('pl', 'en', 'de')}

    assert subtitles.select_subtitle_tracks(info) == {'pl': True, 'en': True}


@pytest.mark.parametrize(
    'info',
    [
        {'id': 'ABC123', 'title': 'Instagram reel', 'extractor_key': 'Instagram'},
        {'subtitles': None, 'automatic_captions': None, 'language': None},
        {'_type': 'playlist', 'entries': [{'id': 'a'}, {'id': 'b'}]},
        {'subtitles': tracks('live_chat'), 'language': 'en'},
    ],
)
def test_nothing_to_take(info):
    assert subtitles.select_subtitle_tracks(info) == {}


def test_subtitleslangs_match_only_the_selected_keys():
    assert subtitles.subtitleslangs_option({'en': False, 'zh-Hans': True}) == ['en', r'zh\-Hans']


@pytest.mark.parametrize(
    ('filename', 'expected'),
    [
        ('Some_Video-abc.en.vtt', 'en'),
        ('Some_Video-abc.en-GB.vtt', 'en-GB'),
        ('Some_Video-abc.cs-orig.srt', 'cs-orig'),
        ('Some_Video-abc.mp4', None),
        ('Some_Video-abc.en.vtt.part', None),
        ('Some_Video-abc.de.vtt', None),
        ('info.json', None),
    ],
)
def test_subtitle_track_of(filename, expected):
    assert subtitles.subtitle_track_of(filename, {'en': False, 'en-GB': False, 'cs-orig': True}) == expected


# YouTube starts each cue with a line of one space or with the rolled-up line before it
YOUTUBE_ASR = '\n'.join(
    [
        'WEBVTT',
        'Kind: captions',
        'Language: en',
        '',
        '00:00:00.000 --> 00:00:02.070 align:start position:0%',
        ' ',
        'AI<00:00:00.320><c> is</c><00:00:00.440><c> not</c><00:00:00.680><c> smart.</c>',
        '',
        '00:00:02.070 --> 00:00:02.080 align:start position:0%',
        'AI is not smart.',
        ' ',
        '',
        '00:00:02.080 --> 00:00:04.030 align:start position:0%',
        'AI is not smart.',
        '&gt;&gt; Why<00:00:02.760><c> not?</c>',
    ]
)


def test_youtube_speech_recognition_captions_give_each_word_its_time():
    assert vtt_words(YOUTUBE_ASR) == [
        CaptionWord(0.0, 'AI'),
        CaptionWord(0.32, 'is'),
        CaptionWord(0.44, 'not'),
        CaptionWord(0.68, 'smart.'),
        CaptionWord(2.08, 'Why', speaker_change=True),
        CaptionWord(2.76, 'not?'),
    ]


def test_words_of_manual_subtitles_are_spread_over_their_cue():
    content = (
        'WEBVTT\n\n'
        '00:00:01.000 --> 00:00:03.000\n- Did you &amp; Kai go?\n\n'
        '00:00:03.000 --> 00:00:04.000\n- Did you &amp; Kai go?\n\n'
        '00:00:04.000 --> 00:00:05.000\n<i>Yes.</i>\n'
    )

    assert vtt_words(content) == [
        CaptionWord(1.0, 'Did', speaker_change=True),
        CaptionWord(1.4, 'you'),
        CaptionWord(1.8, '&'),
        CaptionWord(2.2, 'Kai'),
        CaptionWord(2.6, 'go?'),
        CaptionWord(4.0, 'Yes.'),
    ]
