# webcam.py — webcam frame capture for slowagent.
#
# Two source modes share a single interface:
#   - "http://..." / "https://..."  → fetch each frame from a CGI camera
#                                       (same convention as the existing
#                                       Applications/Camera slowtask).
#   - "file:///abs/path/to/dir"     → cycle through the JPEG/PNG files in a
#                                       local directory.  Used for tests and
#                                       the bundled demo so it runs without
#                                       real hardware.
#
# `get()` returns raw image bytes.  Disk persistence is the slowtask's job —
# the slowtask wipes the batch directory at the start of each cycle and
# writes captured frames there with timestamped names, so the cycler panel
# in the dashboard always shows a coherent batch.

import os
import time as _t
import logging
import urllib.parse

try:
    import slowpy.control
    _have_slowpy_http = True
except ImportError:
    _have_slowpy_http = False


_IMAGE_EXTS = ('.jpg', '.jpeg', '.png')


class WebcamSource:
    """Base class — fetches a single frame as bytes.

    Subclasses set:
      - `self.source` — the original URL/path so callers can detect when the
        source has changed and reopen the stream.
      - `self.display_dir` — local directory the dashboard should read from
        for its cycling-frames display.  None if the source has no on-disk
        representation.
    """

    source: str = ''
    display_dir: str = None

    def get(self) -> bytes:
        raise NotImplementedError

    def list_frames(self) -> list:
        """Sorted (filename, mtime) for files currently visible on disk.
        Empty list if the source has no on-disk view."""
        if not self.display_dir or not os.path.isdir(self.display_dir):
            return []
        return sorted(
            (f, os.path.getmtime(os.path.join(self.display_dir, f)))
            for f in os.listdir(self.display_dir)
            if f.lower().endswith(_IMAGE_EXTS)
        )

    def frame_count(self) -> int:
        """Number of distinct frames currently available on disk."""
        return len(self.list_frames()) or 1

    def close(self):
        pass


class _HTTPWebcam(WebcamSource):
    """HTTP webcam.  Wraps `slowpy.control.ControlSystem().http(url)` so it
    plays nicely with the slowtask runtime.

    `get()` is intentionally side-effect-free — just fetches and returns the
    bytes.  The slowtask saves frames to `display_dir` itself so it can
    enforce batch semantics (wipe-then-fill) rather than rolling cap.
    """

    def __init__(self, url: str, display_dir: str = None):
        if not _have_slowpy_http:
            raise RuntimeError("slowpy.control is required for HTTP webcams")
        self.source = url
        self.display_dir = display_dir
        self._http = slowpy.control.ControlSystem().http(url)

        if self.display_dir:
            try:
                os.makedirs(self.display_dir, exist_ok=True)
            except OSError as e:
                logging.warning("slowagent.webcam: cannot create %s: %s",
                                self.display_dir, e)
                self.display_dir = None

    def get(self) -> bytes:
        return self._http.get()


class _DirectoryWebcam(WebcamSource):
    """Cycles through image files in a directory.  Used for tests and demos
    — no real camera required.  Read-only: never writes to the directory."""

    def __init__(self, path: str):
        if not os.path.isdir(path):
            raise FileNotFoundError(f"webcam directory not found: {path}")
        self.source = path
        self.display_dir = path     # the source IS the display dir
        self._dir = path
        self._idx = 0
        self._files = self._scan()
        if not self._files:
            raise FileNotFoundError(f"no images in {path} (looking for {_IMAGE_EXTS})")

    def _scan(self):
        return sorted(
            os.path.join(self._dir, f)
            for f in os.listdir(self._dir)
            if f.lower().endswith(_IMAGE_EXTS)
        )

    def get(self) -> bytes:
        # Re-scan periodically so newly-dropped files appear.
        if self._idx % 10 == 0:
            files = self._scan()
            if files:
                self._files = files
        if not self._files:
            raise RuntimeError(f"no images in {self._dir}")
        path = self._files[self._idx % len(self._files)]
        self._idx += 1
        with open(path, 'rb') as f:
            return f.read()


def open_webcam(source: str, *, display_dir: str = None) -> WebcamSource:
    """Factory.  Resolves a source URL to the right WebcamSource.

    Supported forms:
        http://host/path           → HTTP CGI camera
        https://host/path          → HTTPS CGI camera
        file:///abs/path           → directory of frames (abs path)
        file://./rel/path          → directory of frames (rel path)
        /abs/path  or  rel/path    → directory of frames (no scheme)

    For HTTP sources, `display_dir` is recorded so callers know where to
    save batch frames.  For directory sources, the source IS the display
    dir and the `display_dir` argument is ignored.
    """
    if not source:
        raise ValueError("webcam source is empty")

    parsed = urllib.parse.urlparse(source)
    scheme = parsed.scheme.lower()

    if scheme in ('http', 'https'):
        logging.info("slowagent.webcam: HTTP source %s (display_dir=%s)",
                     source, display_dir)
        return _HTTPWebcam(source, display_dir=display_dir)

    if scheme == 'file':
        # urlparse parses `file://./foo` as netloc=. + path=/foo, which
        # discards the dot.  Reconstruct from the original string.
        path = source[len('file://'):] if source.startswith('file://') else source[len('file:'):]
        path = path.lstrip('/') if not source.startswith('file:///') else '/' + path.lstrip('/')
        path = os.path.expanduser(path)
        logging.info("slowagent.webcam: directory source %s", path)
        cam = _DirectoryWebcam(path)
        cam.source = source     # preserve the original URL for change-detection
        return cam

    if scheme == '':
        path = os.path.expanduser(source)
        logging.info("slowagent.webcam: directory source %s", path)
        cam = _DirectoryWebcam(path)
        cam.source = source
        return cam

    raise ValueError(f"unsupported webcam scheme: {source!r}")
