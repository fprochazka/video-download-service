# Optional yt-dlp extractor configuration
# Copy this file to extractor_config.py and customize as needed
#
# This file allows you to add custom extractor arguments for specific sites
# without modifying the main application code.
#
# See yt-dlp documentation for available extractor arguments:
# https://github.com/yt-dlp/yt-dlp#extractor-arguments

EXTRACTOR_ARGS = {
    # Example: Skip certain format types for a site
    # 'somesite': {
    #     'skip': ['hls', 'dash'],
    # },

    # Example: Force specific player client
    # 'somesite': {
    #     'player_client': ['android', 'web'],
    # },

    # Example: Use specific API
    # 'somesite': {
    #     'api': ['graphql'],
    # },
}

# Additional yt-dlp options can be configured here
# See: https://github.com/yt-dlp/yt-dlp#usage-and-options
YTDLP_OPTS = {
    # Example: Retry settings for flaky connections
    # 'retries': 3,
    # 'fragment_retries': 3,
    # 'socket_timeout': 30,

    # Example: Rate limiting
    # 'ratelimit': 1000000,  # bytes per second
    # 'sleep_interval': 5,
}
