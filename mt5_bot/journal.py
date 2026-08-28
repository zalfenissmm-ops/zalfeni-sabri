"""CSV trade journal.

One row per event so the file can be opened in a spreadsheet and the day's
result reconciled against the broker statement.
"""

import csv
import os
import time

_COLUMNS = [
    "timestamp", "event", "symbol", "side", "volume", "price",
    "sl", "tp", "target_usd", "risk_usd", "profit_usd", "note",
]


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._ensure_header()

    def _ensure_header(self) -> None:
        """Write the header for a new or empty file, and leave an existing
        journal alone so a restart appends to the same day's records."""
        if not self.path:
            return
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", newline="") as f:
            csv.writer(f).writerow(_COLUMNS)

    def write(self, event: str, **fields) -> None:
        if not self.path:
            return
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "event": event,
        }
        row.update({k: v for k, v in fields.items() if k in _COLUMNS})
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writerow(row)
