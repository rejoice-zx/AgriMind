"""Short-lived storage probe; no business filenames or provider calls."""
from pathlib import Path
import tempfile
import threading
import time

_cache = {}
_lock = threading.Lock()
PROBE_TTL_SECONDS = 30


def storage_readiness(directories):
    result = {}
    with _lock:
        now = time.monotonic()
        for label, directory in directories.items():
            path = Path(directory)
            key = str(path.absolute())
            prior = _cache.get(key)
            if prior and now - prior[0] < PROBE_TTL_SECONDS:
                state = prior[1]
            else:
                try:
                    if not path.is_dir():
                        state = 'missing'
                    else:
                        # Owned temporary file, removed on close on Windows as well.
                        with tempfile.TemporaryFile(prefix='agri-ready-', dir=path) as handle:
                            handle.write(b'readiness'); handle.flush()
                        state = 'writable'
                except OSError:
                    state = 'unwritable'
                _cache[key] = (now, state)
            result[label] = state
    return result
