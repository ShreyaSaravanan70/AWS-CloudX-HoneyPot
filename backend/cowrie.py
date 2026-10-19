"""Adapter to the same parser module deployed to the Cowrie Lambda."""

import importlib.util
from pathlib import Path


_parser_path = Path(__file__).resolve().parents[1] / "Lambda Functions" / "CowrieLogParser.py"
if not _parser_path.is_file():
    _parser_path = Path("/app/CowrieLogParser.py")
_spec = importlib.util.spec_from_file_location("cowrie_log_parser", _parser_path)
_parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parser)


class TooManyCowrieEvents(ValueError):
    """Raised when an upload exceeds the configured parsed-event limit."""


def expected_events(text, max_events=None):
    """Return unique keys using production parsing; optionally cap parsed rows."""
    sessions = {}
    keys = set()
    parsed_count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = _parser.parse_log_line(line, sessions)
        except Exception:
            continue
        if entry and entry["timestamp"] and entry["session"] != "unknown":
            parsed_count += 1
            if max_events is not None and parsed_count > max_events:
                raise TooManyCowrieEvents
            keys.add((entry["session"], entry["timestamp"]))
    return keys
