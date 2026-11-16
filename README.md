# Setup Manager v2

Auto-detects and sets up development environments on Windows, with a web
dashboard, an interactive CLI and a "doctor" that explains why a build failed.

Supports Python, Node.js, C# (.NET), Ruby, Make, Git and Docker, plus QoL
extensions (linters, formatters, type checkers, test frameworks, debuggers,
profilers, security scanners).

## Quick start

```bat
run.bat
```

That creates the virtual environment on first run, installs the dependencies
and starts the dashboard on <http://127.0.0.1:8010>.

The dashboard is the main way to use Setup Manager. The terminal menu is
kept as a **backup** (for example when the browser will not open):

```bat
venv\Scripts\python.exe setup_manager.py
```

## Ports

The dashboard prefers port 8010, but it does not insist on it:

- If 8010 is free, the dashboard uses it.
- If 8010 is taken by something else, it moves up to the next free port in
  the range (8010-8099 by default) and says so on startup.
- If a Setup Manager is already running, a second start does not open a
  second dashboard - it reports the address of the one that is already up.

Wherever the dashboard ended up, it writes `logs/server.json` with its port,
pid and URL, and removes that file when it stops. The CLI and the smoke
test read it, so they always find the server wherever it went.

## Layout

```
SetupManagerv2/
  app.py               FastAPI application (the web dashboard and its API)
  ollama_client.py     Client for the local Ollama server
  setup_manager.py     Backwards-compatible entry point for the engine

  core/                The engine
    detector.py        ProjectDetector, QoLExtensionsRegistry
    executor.py        CommandExecutor - runs commands, streams output, cancels
    security.py        SecurityScanner - heuristic source review
    setup.py           Setup - the per-ecosystem setup handlers
    orchestrator.py    ProjectOrchestrator - headless analyse/diagnose
    workspace.py       WorkspaceDetector, ProjectBootstrapper
    cli.py             InteractiveCLI - the terminal UI
    console.py         UTF-8 console fix (Windows consoles are cp1252)
    server_state.py    Where the server is listening, and whether it is alive

  doctor/              Diagnostics and repairs
    knowledge_base.py  Known build failures: signature -> cause -> fix
    diagnostics.py     Machine and build-output checks
    repairs.py         The changes the doctor knows how to make
    log_manager.py     History of doctor runs (logs/)
    build_doctor.py    Runs a whole pass and writes a report (reports/)
    easter_ggs.py      A very small hidden surprise. Harmless.

  vscode/              VS Code integration
    knowledge_base.py  Known VS Code problems and their fixes

  knowledge/           Extra known-failure data, as JSON
  logs/                Server and doctor logs
  reports/             Doctor reports
  static/              The dashboard's HTML, CSS and JavaScript
  scripts/
    smoke_test.py      End-to-end check against a running server
  create_shortcut.py   Puts a "Setup Manager" shortcut on the Desktop
  tests/               The test suite
```

`core/`, `doctor/` and `ollama_client.py` use the standard library only, so the
engine can be used without installing anything.

## The doctor

The doctor answers two questions.

**Is this machine set up correctly?** `python -m doctor`

```
🩺 DOCTOR REPORT
--------------------------------------------------
ok: 13   warnings: 0   failures: 0
```

**Why did my build fail?** Capture the output and match it against the
knowledge base:

```python
from doctor import diagnose_build_output

for finding in diagnose_build_output(build_output):
    print(finding.title, "->", finding.fix)
```

The knowledge base lives in `doctor/knowledge_base.py`, and extra entries can
be added as JSON files in `knowledge/`. Each entry is a signature that appears
in the error text, with the cause and the fix:

```json
{
  "EADDRINUSE": {
    "name": "Port already in use",
    "cause": "Another process is already listening on the port.",
    "fix": "Stop the other process, or start on a different port"
  }
}
```

## Quality checks

Running the quality checks (menu option 6 in the CLI, or **Quality checks** in
the dashboard) reports one of three results per check:

| Result | Meaning |
|---|---|
| `pass` | The tool ran and the check succeeded. |
| `fail` | The tool ran and the check failed. |
| `skip` | The tool is not installed, or the check had nothing to do. |

A test runner that found no tests to run is a skip, not a failure. `pytest`
and `python -m unittest discover` both exit with code 5 in that case, and
exit code 1 when the tests genuinely fail - so a project that uses pytest
does not get a red "unittest failed" for a suite it never had.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `SETUP_MANAGER_HOST` | `127.0.0.1` | Interface the server binds to |
| `SETUP_MANAGER_PORT` | unset | Pin the server to this port. Unset, it takes the first free port in the range below. |
| `SETUP_MANAGER_PORT_RANGE` | `8010-8099` | The ports the dashboard may use, as `start-end`. |
| `SETUP_MANAGER_TOKEN` | random per run | API token; sent as `X-Setup-Token` |
| `SETUP_MANAGER_SAFE_ROOT` | a `Projects` folder in your user profile (set your own in `run.local.bat`) | The only folder the API may touch. A root chosen in the dashboard wins over this. |
| `SETUP_MANAGER_TASKS_DB` | `tasks.db` | Where task history is stored |
| `SETUP_MANAGER_ALLOW_SHELL` | `0` | Allow arbitrary shell commands |
| `SETUP_MANAGER_MAX_TASKS` | `4` | Concurrent background tasks |
| `SETUP_MANAGER_FILE_WATCH` | `1` | `0` turns the sneaky file watch off for good (Start is refused). Otherwise it is off until you press Start watching. |
| `SETUP_MANAGER_FILE_WATCH_MAX_MINUTES` | `120` | The file watch stops itself after this long. It also stops when the page is closed or hidden for about a minute. |
| `SETUP_MANAGER_OLLAMA_MODEL` | picked by RAM (`run.bat`: `qwen2.5-coder:3b`) | Model used by the AI panel. Unset: under 16 GB RAM `qwen2.5-coder:3b`, 16-31 GB `7b`, 32 GB+ `14b`. |
| `SETUP_MANAGER_OLLAMA_ALLOWED_MODELS` | see `ollama_client.py` | Which models may be used |

`SETUP_MANAGER_SAFE_ROOT` matters: every path the API is given is resolved and
checked against it, so a request cannot reach outside the projects folder.

You can also change it in the dashboard: type or **Browse…** to a folder and
click **Set as projects root**. It is saved in `logs/settings.json`. A whole
drive, your home folder itself, Windows and Program Files are refused. Leave
the box empty and click the button to go back to the default.

## Security

The API listens on localhost only and defends itself against the ways a
browser on the same machine can be abused:

- **This machine only** - requests from any address other than 127.0.0.1 or
  ::1 are refused, whatever the Host header says.
- **Host allow-list** - blocks DNS-rebinding attacks.
- **Per-session token** - every request needs `X-Setup-Token`, so another
  website cannot drive the API. No origin replaces the token, including a
  VS Code webview: an extension must send the token like any other client.
- **Origin check** - blocks cross-site requests and websockets.
- **Path validation** - paths are resolved (symlinks, junctions, `..`) and
  must land inside the safe root.
- **Security headers and a CSP** on every response. The page's script lives
  in `static/dashboard.js`; inline scripts and inline event handlers are
  not allowed.

Set `SETUP_MANAGER_TOKEN` explicitly if you want a stable token across
restarts.

## Tests

```bat
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

## The AI panel

The dashboard can ask a local Ollama model about the project you are working
on. The model must be pulled first:

```bat
ollama pull qwen2.5-coder:3b
```

Everything runs on your own PC: no cloud service, no account, no credits.
The **AI** pill at the top (and the status bar at the bottom) says whether it
is ready, and if not, what to do: *AI offline - start Ollama* or *Model ... not
downloaded - run: ollama pull ...*. Click the pill to check again.

## Approving installs and repairs

Before anything is installed or repaired, the dashboard shows the exact
commands it will run and waits for you to click **Approve**. Cancel runs
nothing.

## The doctor learns

**Diagnostics → Doctor repairs** offers the repairs that fit the project. When
a repair works *and* the problem is gone when the doctor checks again, the fix
is added to `knowledge/known_failures.json` so it is recognised next time.
Fixes that did not work are never recorded.

The dropdown only offers models that are both allowed by the configuration and
actually installed on the machine.

## AI safety

The AI panel is a helper, not a driver. In plain words:

- **The model only suggests.** Its replies are shown to you and nothing else.
  Nothing it says is run, saved, or installed by itself.
- **Nothing runs without your click.** Commands only run when you press Run or
  Approve and say OK first.
- **The Run box and AI suggestions are checked.** Setup Manager refuses
  commands that look like malware: downloading and running code, hidden or
  encoded PowerShell, adding programs to start-up, turning off antivirus or the
  firewall, reading saved passwords, SSH keys or browser data, wiping disks, or
  grabbing admin rights. The same check drops risky AI setup suggestions, and
  the doctor neither loads nor learns "known fix" entries whose advice looks
  harmful. Extra known-fix files can add entries but never replace the
  built-in ones.
- **The project's own scripts are not checked.** Install, Launch, make targets
  and setup hooks run the project's code as it is. Only run projects you trust.
- **Project files are treated as untrusted.** A README or log can try to give
  the model orders. The model is told to treat project text as data only, and
  to refuse to help write malware. Files that link outside the project, or that
  are too big, are never sent to it.

This is a safety net, not a guarantee. The check looks for known patterns, and
a harmful command can be disguised. Run the security scan first, and only run
things you understand. Each time you open a folder, a reminder offers to run
the scan for you; you can turn it off for a folder you trust, and it comes back
if the folder is deleted and cloned again.

## Dedication

In memory of JPP, who gave me the strength and the push to go on.

Thanks to Rock, for being a good critic. I couldn't have done it without you.

## License

Setup Manager is free software under the [MIT License](LICENSE). You can use,
copy, change, and share it, including in your own projects, at no cost.

Copyright (c) 2026 E. Guardado.

This project was developed by E. Guardado with assistance from Grok Bot, and
is shared so other people can set up their own tools the same way.
