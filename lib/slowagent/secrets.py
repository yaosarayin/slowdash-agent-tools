# secrets.py — secure API-key loader for slowdash-agent-tools.
#
# Why a dedicated loader?  We want a single, predictable path for credentials
# so they never end up in the slowdash project repo, in container images, or
# in slowtask parameter blocks.  The loader also enforces 0600 permissions
# on the secrets file — accidentally world-readable keys are common, so we
# refuse to read them rather than fail open.
#
# Resolution order (first hit wins):
#   1. ~/.config/slowdash/secrets.toml   (TOML key=value, mode 0600)
#   2. environment variable              (UPPERCASE name, e.g. ANTHROPIC_API_KEY)
#
# Usage:
#     from slowagent import get_secret
#     key = get_secret('anthropic_api_key')

import os
import stat
import logging

try:                       # python 3.11+
    import tomllib
except ImportError:        # python 3.10 fallback
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


SECRETS_PATH = os.path.expanduser('~/.config/slowdash/secrets.toml')


class SecretError(Exception):
    """Raised when a required secret cannot be loaded."""


_cache = None


def _load_file():
    """Parse the secrets TOML file, refusing to read it if it is world-readable."""
    global _cache
    if _cache is not None:
        return _cache

    if not os.path.isfile(SECRETS_PATH):
        _cache = {}
        return _cache

    if tomllib is None:
        raise SecretError(
            "slowagent.secrets needs `tomllib` (Python 3.11+) or `tomli`. "
            "pip install tomli"
        )

    st = os.stat(SECRETS_PATH)
    bad_mode = st.st_mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
    if bad_mode:
        raise SecretError(
            f"{SECRETS_PATH} has insecure permissions "
            f"({oct(stat.S_IMODE(st.st_mode))}). Run: chmod 600 {SECRETS_PATH}"
        )

    with open(SECRETS_PATH, 'rb') as f:
        _cache = tomllib.load(f)
    return _cache


def get_secret(name: str, default=None) -> str:
    """Look up a secret by name. Returns `default` (or raises) if not found.

    Tries the TOML file first, then the environment variable
    (uppercased, e.g. ``anthropic_api_key`` -> ``ANTHROPIC_API_KEY``).
    """
    try:
        data = _load_file()
    except SecretError as e:
        logging.warning("slowagent.secrets: %s", e)
        data = {}

    if name in data and data[name]:
        return data[name]

    env = os.environ.get(name.upper())
    if env:
        return env

    if default is not None:
        return default

    raise SecretError(
        f"secret '{name}' not found. Add it to {SECRETS_PATH} "
        f"or set ${name.upper()}."
    )
