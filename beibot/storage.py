import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


class Store:
    """A JSON file that survives crashes, restarts and git pulls.

    Files live in a data/ directory next to main.py, resolved absolutely so
    the working directory does not matter. Writes go to a temp file and are
    renamed into place, so a process killed mid-write leaves the previous
    version intact rather than a truncated one. The previous version is also
    kept as a .bak and used automatically if the main file is unreadable.
    """

    def __init__(self, filename, default=dict):
        self.name = filename
        self.path = DATA_DIR / filename
        self.backup = DATA_DIR / (filename + ".bak")
        self.legacy = ROOT / filename
        self._default = default

    def load(self):
        for candidate in (self.path, self.backup, self.legacy):
            if not candidate.exists():
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if candidate is not self.path:
                    print(f"[storage] recovered {self.name} from {candidate.name}")
                    self.save(data)
                return data
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[storage] {candidate.name} unreadable: {exc}")

        return self._default()

    def save(self, data):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DATA_DIR / (self.name + ".tmp")

            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            if self.path.exists():
                os.replace(self.path, self.backup)

            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            print(f"[storage] failed to write {self.name}: {exc}")
            return False


class IntKeyStore(Store):
    """Same as Store, but restores top level keys as ints.

    JSON object keys are always strings. Anything keyed by a Discord
    snowflake needs converting back on load or lookups silently miss.
    """

    def load(self):
        raw = super().load()
        if not isinstance(raw, dict):
            return self._default()
        try:
            return {int(k): v for k, v in raw.items()}
        except (ValueError, TypeError):
            print(f"[storage] {self.name} has non integer keys, discarding")
            return self._default()

    def save(self, data):
        return super().save({str(k): v for k, v in data.items()})