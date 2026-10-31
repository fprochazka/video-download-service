import pytest

from video_download import subtitles

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
