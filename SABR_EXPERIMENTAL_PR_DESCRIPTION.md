## Draft: Experimental Browser-Assisted SABR Fallback

**This PR is experimental and is shared for visibility, discussion, and cross-platform testing only. It is not currently requesting merge.**

Some WEB client streams use YouTube's SABR delivery system and may stop after
the first media chunk because the normal Python SABR client cannot maintain
YouTube's changing playback state.

This branch adds an **optional** fallback that lets a local Playwright Chromium
browser handle SABR playback while pytubefix captures the exact stream selected
by the user.

Normal pytubefix behavior is unchanged. The browser fallback runs only when
explicitly enabled with:

```python
sabr_browser_fallback=True
```

## Install

Python 3.8+ is required.

```bash
python -m pip install "pytubefix[sabr-browser] @ git+https://github.com/Muggwomp/pytubefix.git@sabr-fix"
python -m playwright install chromium
```

Playwright downloads Chromium once. Chrome does not need to be installed
separately.

On Ubuntu or other Linux systems without a graphical desktop:

```bash
sudo apt install -y xvfb
```

## Basic Usage

```python
from pytubefix import YouTube

yt = YouTube(
    "https://www.youtube.com/watch?v=VIDEO_ID",
    client="WEB",
    sabr_browser_fallback=True,
)

stream = yt.streams.get_by_itag(136)
stream.download()
```

List streams first if you do not know the available itag:

```python
for stream in yt.streams:
    print(
        stream.itag,
        stream.resolution,
        stream.video_codec or stream.audio_codec,
        stream.is_sabr,
    )
```

On Linux without a desktop, run the script through a virtual display:

```bash
xvfb-run -a python your_script.py
```

## Current Scope and Limitations

- Designed for SABR streams that already appear in `yt.streams`.
- It may help when a WEB SABR download begins but fails later with PoToken or stream-protection errors.
- It does not address initial "Detected as a bot" responses, unavailable videos, or untrusted proxy IPs.
- Video and audio adaptive streams are separate. Users must download both and mux them with FFmpeg when they want a combined file.
- Browser-assisted capture is slower and uses more resources than normal pytubefix downloading.
- The browser dependency is optional and is not imported during ordinary pytubefix use.

## Requested Testing Feedback

Please include:

- Operating system and Python version
- Whether the system has a desktop display, uses `xvfb`, or runs headless
- Video URL and selected itag
- Video/audio codec
- Full traceback or console output
- Whether normal pytubefix succeeded, failed, or stopped partway through download
