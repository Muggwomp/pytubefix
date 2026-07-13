.. _sabr_browser:

Browser-assisted SABR downloads
================================

Some WEB client streams use YouTube's Server-side Adaptive Bitrate (SABR)
protocol and may stop after the first chunk when the normal Python SABR client
cannot satisfy YouTube's changing playback attestation state.

pytubefix offers an optional browser-assisted backend for this case. YouTube's
own browser player performs SABR negotiation while pytubefix captures the exact
itag selected by the caller and writes it through the normal ``Stream.download``
callbacks. The browser backend is opt-in and Playwright is not imported during
ordinary pytubefix use.

Installation
------------

Install the optional dependency and a Chromium browser runtime::

    python -m pip install "pytubefix[sabr-browser]"
    python -m playwright install chromium

The browser-assisted backend requires Python 3.8 or newer.

Chrome or Edge already installed on the system can be selected instead of the
Playwright-managed browser. For example, set
``PYTUBEFIX_SABR_BROWSER_CHANNEL=chrome``.

Usage
-----

Enable the backend on a YouTube instance and use the normal stream API::

    from pytubefix import YouTube

    yt = YouTube(
        "https://www.youtube.com/watch?v=VIDEO_ID",
        client="WEB",
        sabr_browser_fallback=True,
    )
    stream = yt.streams.get_by_itag(136)
    stream.download()

The selected stream remains a single audio-only or video-only representation.
Downloading separate adaptive audio and video streams still requires muxing if
a combined output is desired.

Browser modes
-------------

``PYTUBEFIX_SABR_BROWSER_MODE`` accepts these values:

``auto``
    Use hidden-headed mode on Windows, headed mode when Linux/macOS has a
    display, and headless mode on a display-less host. This is the default.

``hidden-headed``
    Run a real headed Chromium window off-screen. This is generally the most
    reliable mode on Windows.

``headed``
    Show the browser window. This is useful for diagnostics and Linux desktop
    sessions.

``headless``
    Run without a visible browser. YouTube may reject or throttle headless
    playback for some videos.

For dependable background operation on an Ubuntu server, provide a virtual
display so ``auto`` can use the full headed browser engine::

    sudo apt install xvfb
    xvfb-run -a python your_downloader.py

Additional environment settings include:

``PYTUBEFIX_SABR_BROWSER_CHANNEL``
    Playwright browser channel such as ``chrome`` or ``msedge``.

``PYTUBEFIX_SABR_BROWSER_PATH``
    Explicit browser executable path.

``PYTUBEFIX_SABR_BROWSER_CAPTURE_TIMEOUT``
    Maximum capture time in seconds. The default is 240.

``PYTUBEFIX_SABR_BROWSER_PLAYBACK_RATE``
    Browser playback rate used while acquiring segments. The default is 16.

Safety and resource behavior
----------------------------

The destination is not populated until every segment of the exact requested
itag has arrived. Captured data is kept in an ordered spool which moves to a
temporary file after 64 MiB, avoiding unbounded memory use for large streams.
If the browser chooses a different representation or capture is incomplete,
pytubefix raises ``SABRError`` instead of silently returning another format or
a partial file.

H.264 and AAC use parsed UMP media segments. AV1, VP9, and Opus use a bounded
browser SourceBuffer queue because their SABR responses may remain open long
enough to block ``response.body()``. The SourceBuffer path checks the player's
active ``fmt`` or ``afmt`` value on every append and accepts bytes only for the
exact requested itag.

Raw VP9 WebM output contains the complete media timeline but may not contain a
final cues/footer section because browsers do not append that non-playback
index data to MediaSource. Players and FFmpeg can decode the stream, though
strict WebM tooling may report that the file ended without a footer. Remuxing
with FFmpeg rebuilds the container metadata when required.

When ``proxies`` are supplied to ``YouTube``, the browser backend passes the
same HTTP(S) proxy and credentials to Playwright. Invalid or unsupported proxy
mappings fail with ``SABRError`` rather than silently opening a direct browser
connection.
