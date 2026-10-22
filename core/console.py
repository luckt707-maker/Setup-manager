import sys


def _force_utf8_console():
    """Windows consoles default to cp1252, so the emoji used in progress output
    raise UnicodeEncodeError and kill the process. Force UTF-8 on the standard
    streams; fall back to replacing unencodable characters."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
