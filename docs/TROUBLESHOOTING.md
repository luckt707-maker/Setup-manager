# Troubleshooting

## The scanner said CRITICAL over `fmt.Sprintf`

PhoneInfoga and other Go projects can use `fmt.Sprintf`. An older scan
treated that the same as the unsafe C function `sprintf`, scored each
hit, and labeled the folder "63, CRITICAL" even when Microsoft Defender
found nothing.

That is fixed in 1.2.0. C buffer-overflow rules only apply to C and C++
files, documentation files are skipped, and when Defender is clean a high
heuristic score is shown as "Review suggested" instead of "CRITICAL."
Re-scan the folder in this version; a Go file that only uses
`fmt.Sprintf` should come back clean.

If Defender itself reports a threat, treat that as a real alert.

## Ollama is offline

The AI pill and the status bar will say `AI offline - start Ollama` (or
that the model is not downloaded). Setup Manager still runs without
Ollama. To use the AI panel:

1. Start Ollama on this PC.
2. Pull the model if you do not have it, for example
   `ollama pull qwen2.5-coder:3b`.
3. Click the AI pill to check again.

`run.bat` defaults the model to `qwen2.5-coder:3b`. You can change that
with `SETUP_MANAGER_OLLAMA_MODEL` in `run.local.bat`.

## Port 8010 is busy (including TIME_WAIT)

The dashboard prefers port 8010 on `127.0.0.1`. If that port is taken, it
moves to the next free port in 8010-8099 and prints a note in the
`run.bat` window. If Setup Manager is already running, a second start
reuses that instance.

After you close the app, Windows can keep the port in `TIME_WAIT` for a
short time. The next start may then use 8011 (or the next free port)
until 8010 is released. That is normal. Wait a moment and start again if
you want 8010, or set `SETUP_MANAGER_PORT` in `run.local.bat` to pin a
port. If you pin a port that is still in use by another program, startup
stops and tells you to free it or choose a different port.

The live address is written to `logs/server.json` while the server is
running.

## Defender says a scan is already in progress

A custom Microsoft Defender scan can fail if Defender is already scanning
something else. Wait for that scan to finish, then try again. Do not start
several Setup Manager scans at the same time.

If Defender is disabled or its scan commands are missing, Setup Manager
treats the scan as unavailable and, by default, continues. Set
`SETUP_MANAGER_REQUIRE_DEFENDER=1` if you want those actions to stop
instead.

## `tasks.db-wal` is large

`tasks.db-wal` is SQLite's write-ahead log, next to `tasks.db` in the
Setup Manager folder. It can grow if the app was left running for a long
time or was not shut down cleanly.

In 1.2.0 the app checkpoints and truncates that log when it starts and
after it deletes old finished tasks (older than one hour). Close Setup
Manager, start it once, and the file should shrink. Do not delete
`tasks.db` or the `-wal` / `-shm` files while the app is open.

## The virtual environment is broken

If `run.bat` fails inside `venv`, or Python reports missing or mixed
standard-library modules, the environment may be left over from another
Python version.

1. Close Setup Manager.
2. Rename the `venv` folder, for example to `venv.old-2026-11-16`.
3. Run `run.bat` again. It will create a new `venv` and reinstall the
   packages.

If that works, you can delete the renamed folder later. Do not move or
rename `venv` while Setup Manager is running.
