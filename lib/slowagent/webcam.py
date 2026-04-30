# webcam.py — webcam frame capture for slowagent.
#
# Two source modes share a single interface:
#   - "http://..." or "https://..."  → fetch each frame from a CGI camera
#                                        (same convention as the existing
#                                        Applications/Camera slowtask).
#   - "file:///abs/path/to/dir"      → cycle through the JPEG/PNG files in a
#                                        local directory.  Used for the
#                                        ExampleProject so the demo runs
#                                        without real hardware.
#
# Both return raw image bytes; the caller is responsible for sending them to
# the LLM and *not* persisting them after extraction (per the security model
# in the README).

import os
import re
import time
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
        """Return a sorted list of (filename, mtime) for files currently
        available on disk.  Empty list if the source has no on-disk view."""
        if not self.display_dir or not os.path.isdir(self.display_dir):
            return []
        return sorted(
            (f, os.path.getmtime(os.path.join(self.display_dir, f)))
            for f in os.listdir(self.display_dir)
            if f.lower().endswith(_IMAGE_EXTS)
        )

    def frame_count(self) -> int:
        """How many distinct frames are available right now.  Used at
        startup to size the initial buffer for the first LLM call."""
        return len(self.list_frames()) or 1

    def close(self):
        pass


class _HTTPWebcam(WebcamSource):
    """HTTP webcam.  Wraps slowpy.control.ControlSystem().http(url) so it
    plays nicely with the slowtask runtime (which already sets up its own
    event loop).

    If `display_dir` is set, every successful capture is also written there
    with a timestamped filename, and the directory is pruned to keep only
    the last `keep` files.  The dashboard reads from that directory to
    cycle through recent frames."""

    def __init__(self, url: str, display_dir: str = None, keep: int = 10):
        if not _have_slowpy_http:
            raise RuntimeError("slowpy.control is required for HTTP webcams")
        self.source = url
        self.display_dir = display_dir
        self._keep = max(1, int(keep))
        self._http = slowpy.control.ControlSystem().http(url)

        if self.display_dir:
            try:
                os.makedirs(self.display_dir, exist_ok=True)
            except OSError as e:
                logging.warning("slowagent.webcam: cannot create %s: %s",
                                self.display_dir, e)
                self.display_dir = None

    def get(self) -> bytes:
        blob = self._http.get()
        if self.display_dir:
            self._save_and_prune(blob)
        return blob

    def _save_and_prune(self, blob: bytes):
        """Write `blob` to display_dir with a timestamped name, then trim
        the directory back down to self._keep files (oldest first)."""
        import time as _t
        ext = '.png' if blob.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg'
        fname = _t.strftime('%y%m%d-%H%M%S') + ext
        path = os.path.join(self.display_dir, fname)
        try:
            with open(path, 'wb') as f:
                f.write(blob)
        except OSError as e:
            logging.warning("slowagent.webcam: cannot save frame %s: %s", path, e)
            return

        # Prune. list_frames() returns newest-last by mtime ordering after
        # the sort; remove from the front (oldest) until we're at `keep`.
        frames = sorted(
            (os.path.getmtime(os.path.join(self.display_dir, f)), f)
            for f in os.listdir(self.display_dir)
            if f.lower().endswith(_IMAGE_EXTS)
        )
        excess = len(frames) - self._keep
        for _, f in frames[:max(0, excess)]:
            try:
                os.remove(os.path.join(self.display_dir, f))
            except OSError:
                pass


class _DirectoryWebcam(WebcamSource):
    """Cycles through image files in a directory.  Useful for tests and the
    bundled example project — no real camera required."""

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
        # Re-scan periodically so newly-dropped files show up.
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


def open_webcam(source: str, *, display_dir: str = None, keep: int = 10) -> WebcamSource:
    """Factory.  Resolves a source URL to the right WebcamSource.

    Supported forms:
        http://host/path          → HTTP CGI camera (rolling capture-to-disk)
        https://host/path         → HTTPS CGI camera (rolling capture-to-disk)
        file:///abs/path          → directory of frames (abs path, read-only)
        file://./rel/path         → directory of frames (rel path, read-only)
        /abs/path  or  rel/path   → directory of frames (no scheme, read-only)

    For HTTP sources, `display_dir` is the directory where each capture is
    saved with a rolling cap of `keep` files.  Defaults to None (memory only).
    """
    if not source:
        raise ValueError("webcam source is empty")

    parsed = urllib.parse.urlparse(source)
    scheme = parsed.scheme.lower()

    if scheme in ('http', 'https'):
        logging.info("slowagent.webcam: HTTP source %s (display_dir=%s, keep=%d)",
                     source, display_dir, keep)
        cam = _HTTPWebcam(source, display_dir=display_dir, keep=keep)
        cam.source = source
        return cam

    if scheme == 'file':
        # urlparse parses `file://./foo` as netloc=. + path=/foo, which
        # discards the dot.  Reconstruct from the original string instead.
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
