import json
import os
from pathlib import Path

# The folder this file sits in. For bots/avenoir/storage.py that is
# bots/avenoir/, so data lands in bots/avenoir/data/ and stays separate
# from every other bot in the repo.
BOT_ROOT = Path(__file__).resolve().parent

_data_dir = BOT_ROOT / "data"


def configure(path):
    """Point storage at a different folder.

    Only needed if you later move this file into a shared core/ package and
    launch bots from a single runner. Call it before any cog is imported.
    """
    global _data_dir
    _data_dir = Path(path).resolve()


def data_dir():
    return _data_dir


class Store:
    """A JSON file that survives crashes, restarts and git pulls.

    Writes go to a temp file which is flushed, fsynced, then renamed into
    place. Rename is atomic on every OS we care about, so a process killed
    mid-write leaves the previous file intact rather than a truncated one.

    The version being replaced is kept as a .bak and used automatically if
    the main file ever comes back unreadable.
    """

    def __init__(self, filename, default=dict):
        self.name = filename
        self._default = default

    # Paths are resolved on access, not in __init__, so configure() works
    # no matter what order modules happen to import in.
    @property
    def path(self):
        return _data_dir / self.name

    @property
    def backup(self):
        return _data_dir / (self.name + ".bak")

    @property
    def legacy(self):
        """Older layout, where JSON sat next to the bot instead of in data/."""
        return BOT_ROOT / self.name

    def load(self):
        for candidate in (self.path, self.backup, self.legacy):
            if not candidate.exists():
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if candidate != self.path:
                    print(f"[storage] recovered {self.name} from {candidate.name}")
                    self.save(data)
                return data
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[storage] {candidate.name} unreadable: {exc}")

        return self._default()

    def save(self, data):
        try:
            _data_dir.mkdir(parents=True, exist_ok=True)
            tmp = _data_dir / (self.name + ".tmp")

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