"""Launch every bot in this folder as its own process.

Each bot keeps its own interpreter, so their identically named modules
(prefixes, storage) and their data folders never collide. A crash in one
bot does not take the others down, and the supervisor restarts it.
"""

import asyncio
import contextlib
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

RESTART_DELAY = 5
MAX_DELAY = 120
HEALTHY_AFTER = 30

_shutting_down = False
_processes = {}


def discover_bots():
    """Every subfolder holding a main.py, in a stable order."""
    found = []
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_dir() and (entry / "main.py").is_file():
            found.append(entry)
    return found


def log(name, text):
    print(f"[{name}] {text}", flush=True)


async def pump_output(name, stream):
    while True:
        line = await stream.readline()
        if not line:
            return
        log(name, line.decode("utf-8", errors="replace").rstrip())


async def run_once(folder):
    """Start one bot and wait for it to exit. Returns its exit code."""
    name = folder.name

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "main.py",
        cwd=str(folder),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    _processes[name] = process

    try:
        await pump_output(name, process.stdout)
        return await process.wait()
    finally:
        _processes.pop(name, None)


async def supervise(folder):
    name = folder.name
    delay = RESTART_DELAY

    while not _shutting_down:
        started = time.monotonic()
        log(name, "starting")

        try:
            code = await run_once(folder)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(name, f"could not start: {type(exc).__name__}: {exc}")
            code = -1

        if _shutting_down:
            return

        alive_for = time.monotonic() - started

        if alive_for >= HEALTHY_AFTER:
            delay = RESTART_DELAY
        else:
            delay = min(delay * 2, MAX_DELAY)

        log(name, f"exited with code {code} after {alive_for:.0f}s")
        log(name, f"restarting in {delay}s")

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return


def request_shutdown():
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    print("Stopping all bots.", flush=True)

    for name, process in list(_processes.items()):
        if process.returncode is None:
            log(name, "sending terminate")
            with contextlib.suppress(ProcessLookupError):
                process.terminate()


async def main():
    folders = discover_bots()

    if not folders:
        print("No bot folders found. Each bot needs its own folder with a main.py.")
        return

    print(f"Supervising: {', '.join(f.name for f in folders)}", flush=True)

    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)

    tasks = [asyncio.create_task(supervise(f), name=f.name) for f in folders]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

    for name, process in list(_processes.items()):
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                log(name, "did not stop in time, killing")
                with contextlib.suppress(ProcessLookupError):
                    process.kill()

    print("All bots stopped.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass