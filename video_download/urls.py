"""Validation of submitted URLs and the reduction of YouTube playlist links to a single video"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.youtube import YoutubeIE

PLAYLIST_QUERY_PARAMS = {'list', 'index', 'start_radio', 'pp', 'playnext'}
PLAYLIST_REJECTED_MESSAGE = 'Playlists and channels are not supported, paste a link to a single video.'


class InvalidDownloadUrl(ValueError):
    pass


def detect_extractor(url: str) -> type:
    """First yt-dlp extractor that claims the URL (the generic extractor claims everything)"""
    return next(ie for ie in gen_extractor_classes() if ie.suitable(url))


def strip_playlist_params(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query if k not in PLAYLIST_QUERY_PARAMS]
    if len(kept) == len(query):
        return url
    return urlunsplit(parts._replace(query=urlencode(kept)))


def resolve_download_url(url: str) -> tuple[str, str]:
    """Return (url to pass to yt-dlp, extractor key), or raise InvalidDownloadUrl.

    YouTube links that carry a playlist next to a video (mixes, "play all", music radio) are reduced to the video.
    Pure playlist and channel links are rejected. Other sites are left untouched, an Instagram carousel is a playlist
    too and has to keep working.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise InvalidDownloadUrl('Enter a valid http(s) URL.')

    extractor = detect_extractor(url)
    if not extractor.ie_key().startswith('Youtube'):
        return url, extractor.ie_key()

    stripped = strip_playlist_params(url)
    if YoutubeIE.suitable(stripped):
        return stripped, YoutubeIE.ie_key()
    stripped_extractor = detect_extractor(stripped)
    if stripped_extractor._RETURN_TYPE == 'video':  # e.g. clips
        return stripped, stripped_extractor.ie_key()
    raise InvalidDownloadUrl(PLAYLIST_REJECTED_MESSAGE)
