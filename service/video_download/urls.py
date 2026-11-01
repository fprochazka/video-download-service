"""Validation of submitted URLs: links to collections are rejected, YouTube playlist links reduced to the video"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from yt_dlp.extractor import gen_extractor_classes, get_info_extractor
from yt_dlp.extractor.youtube import YoutubeIE

PLAYLIST_QUERY_PARAMS = {'list', 'index', 'start_radio', 'pp', 'playnext'}
# Query parameters that only track where a link was shared from; a link of the same page without them is the same video
TRACKING_QUERY_PARAMS = {'fbclid', 'gclid', 'igsh', 'igshid', 'si', 'feature'}
PLAYLIST_REJECTED_MESSAGE = (
    'Playlists, channels, profiles and other collections are not supported, paste a link to a single video or post.'
)


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

    A link whose extractor returns only collections (profiles, channels, albums, playlists) is rejected, even when the
    collection holds one video. Extractors of a single post that may carry several videos, like an Instagram carousel
    or a tweet, declare "any" and are allowed.
    YouTube links that carry a playlist next to a video (mixes, "play all", music radio) are reduced to the video, and
    pure YouTube playlist and channel links are rejected; YouTube's tab extractor declares "any", so it needs this rule.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise InvalidDownloadUrl('Enter a valid http(s) URL.')

    extractor = detect_extractor(url)
    if not extractor.ie_key().startswith('Youtube'):
        if getattr(extractor, '_RETURN_TYPE', None) == 'playlist':
            raise InvalidDownloadUrl(PLAYLIST_REJECTED_MESSAGE)
        return url, extractor.ie_key()

    stripped = strip_playlist_params(url)
    if YoutubeIE.suitable(stripped):
        return stripped, YoutubeIE.ie_key()
    stripped_extractor = detect_extractor(stripped)
    if stripped_extractor._RETURN_TYPE == 'video':  # e.g. clips
        return stripped, stripped_extractor.ie_key()
    raise InvalidDownloadUrl(PLAYLIST_REJECTED_MESSAGE)


def video_key(download_url: str, extractor: str) -> str:
    """The identity of the video a resolved link points to, derived from the link alone without a request.

    <extractor>:<video id> when the extractor's URL pattern names the id, so youtu.be/X, watch?v=X&t=42s and /shorts/X
    are one video, and so are an Instagram /p/X and /reel/X. Otherwise url:<the normalized link>.
    """
    try:
        video_id = get_info_extractor(extractor).get_temp_id(download_url)
    except Exception:
        video_id = None
    if video_id and extractor != 'Generic':
        return f'{extractor}:{video_id}'
    return 'url:' + normalize_url(download_url)


def normalize_url(url: str) -> str:
    """The link with a lowercase scheme and host, without its fragment and the tracking parameters"""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_QUERY_PARAMS and not k.startswith('utm_')
    ]
    return urlunsplit(
        parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), query=urlencode(query), fragment='')
    )
