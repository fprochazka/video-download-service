import pytest

from video_download import languages, storage, transcription

CZECH = 'Dobrý den, dnes si povídáme o motorech a o tom, jak se dělají převodovky v Mladé Boleslavi.'
RUSSIAN = 'Добрый день, сегодня мы говорим о двигателях и о том, как делают коробки передач в Младе-Болеславе.'
JAPANESE = (
    'こんにちは、今日はエンジンとギアボックスの作り方についてお話しします。ムラダー・ボレスラフの工場で働いています。'
)


@pytest.mark.parametrize(
    ('text', 'expected', 'share'),
    [
        (CZECH, ['cs'], 1.0),
        (CZECH, ['cs', 'en'], 1.0),
        (RUSSIAN, ['cs'], 0.0),
        (RUSSIAN, ['ru'], 1.0),
        (JAPANESE, ['ja'], 1.0),
        (JAPANESE, ['en'], 0.0),
        # A Czech answer with a quoted Russian title still passes
        (CZECH * 3 + ' Добрый день.', ['cs'], pytest.approx(0.95, abs=0.03)),
        # Too short to tell, or a language whose script is not known
        ('Добрый', ['cs'], None),
        (RUSSIAN, ['xx'], None),
        (RUSSIAN, [], None),
    ],
)
def test_share_of_letters_in_the_scripts_of_the_expected_languages(text, expected, share):
    assert languages.expected_share(text, expected) == share


def test_language_codes_from_untrusted_values():
    assert [languages.language_code(v) for v in ('cs', 'en-US', 'en-orig', 'CS', '', None, 'x" ignore', 'zh-Hans')] == [
        'cs',
        'en',
        'en',
        'cs',
        None,
        None,
        None,
        'zh',
    ]


def subtitle(name, lang, auto):
    return {'name': name, 'kind': 'subtitle', 'lang': lang, 'auto': auto}


def test_expected_languages_leave_out_machine_translations():
    artifacts = [
        {'name': 'a.mp4', 'kind': 'media'},
        subtitle('a.cs-orig.vtt', 'cs-orig', True),
        subtitle('a.en.vtt', 'en', True),
        subtitle('a.de.vtt', 'de', False),
    ]

    assert transcription.expected_languages({'language': 'cs'}, artifacts) == ['cs', 'de']
    assert transcription.expected_languages({}, artifacts[:3]) == ['cs']
    assert transcription.expected_languages({}, []) == []


def test_the_system_instruction_names_the_expected_languages():
    one = transcription.system_instruction(['cs'])
    two = transcription.system_instruction(['cs', 'en', 'xx'])

    assert one == (
        transcription.SYSTEM_INSTRUCTION
        + '\n- The speech is in Czech (cs). Write each language as it is spoken: never '
        'translate or transliterate it, and keep its diacritics.'
    )
    assert '- The speech is expected to be in Czech (cs), English (en) or xx. Write each language' in two
    assert transcription.system_instruction([]) == transcription.SYSTEM_INSTRUCTION


def test_a_job_with_english_subtitles_expects_english(downloads_dir):
    from tests.test_transcription import write_job

    job_dir = write_job(downloads_dir)

    context = transcription.build_context(job_dir, storage.load_metadata(job_dir))

    assert context.languages == ['en']
    assert 'The speech is in English (en).' not in transcription.build_prompt(context, 120)
