"""The languages a recording is expected to be in, for the prompt, and the scripts they are written in, to catch an
answer that came back translated or transliterated"""

import re
import unicodedata

LANGUAGE_CODE_RE = re.compile(r'^[a-z]{2,3}$')

NAMES = {
    'cs': 'Czech',
    'sk': 'Slovak',
    'en': 'English',
    'de': 'German',
    'pl': 'Polish',
    'hu': 'Hungarian',
    'fr': 'French',
    'es': 'Spanish',
    'it': 'Italian',
    'pt': 'Portuguese',
    'nl': 'Dutch',
    'uk': 'Ukrainian',
    'ru': 'Russian',
    'el': 'Greek',
    'ja': 'Japanese',
    'zh': 'Chinese',
    'ko': 'Korean',
    'ar': 'Arabic',
}

# The scripts of a language's letters, by the first word of their Unicode character names
SCRIPTS = {
    'ru': {'CYRILLIC'},
    'uk': {'CYRILLIC'},
    'bg': {'CYRILLIC'},
    'be': {'CYRILLIC'},
    'mk': {'CYRILLIC'},
    'sr': {'CYRILLIC', 'LATIN'},
    'el': {'GREEK'},
    'ar': {'ARABIC'},
    'fa': {'ARABIC'},
    'ur': {'ARABIC'},
    'he': {'HEBREW'},
    'zh': {'CJK'},
    'ja': {'CJK', 'HIRAGANA', 'KATAKANA', 'KATAKANA-HIRAGANA'},
    'ko': {'HANGUL', 'CJK'},
    'hi': {'DEVANAGARI'},
    'th': {'THAI'},
}
# The languages of the Latin script that the check knows; the scripts of any other language are unknown
LATIN = frozenset(
    'cs sk en de pl hu fr es it pt nl sv da no fi et lv lt sl hr ro tr ca gl eu ga cy is mt sq id ms vi tl sw af'.split()
)

# An answer is checked once it has this many letters, and fails when fewer than this share are in an expected script
MIN_LETTERS = 50
MIN_EXPECTED_SHARE = 0.5


def language_code(value) -> str | None:
    """The base language code of a value from info.json or a subtitle track key, None when it is not one"""
    code = str(value or '').split('-')[0].lower()
    return code if LANGUAGE_CODE_RE.match(code) else None


def describe(languages: list[str]) -> str:
    return ', '.join(f'{NAMES[code]} ({code})' if code in NAMES else code for code in languages)


def expected_scripts(languages: list[str]) -> set[str] | None:
    """The scripts the languages are written in, None when one of them is not known"""
    scripts = set()
    for code in languages:
        if code in LATIN:
            scripts.add('LATIN')
        elif code in SCRIPTS:
            scripts |= SCRIPTS[code]
        else:
            return None
    return scripts


def script_of(letter: str) -> str:
    return unicodedata.name(letter, 'UNKNOWN').split(' ')[0]


def expected_share(text: str, languages: list[str]) -> float | None:
    """The share of the letters of the text that are in a script of the languages; None when the languages, or their
    scripts, are not known, or the text has too few letters to tell"""
    scripts = expected_scripts(languages) if languages else None
    letters = [char for char in text if char.isalpha()]
    if not scripts or len(letters) < MIN_LETTERS:
        return None
    return sum(script_of(letter) in scripts for letter in letters) / len(letters)
