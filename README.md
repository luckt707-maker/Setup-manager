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

To run the interactive terminal UI instead:

```bat
venv\Scripts\python.exe setup_manager.py
```

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
| `SETUP_MANAGER_PORT` | `8010` | Port the server listens on |
| `SETUP_MANAGER_TOKEN` | random per run | API token; sent as `X-Setup-Token` |
| `SETUP_MANAGER_SAFE_ROOT` | `C:\dev` | The only folder the API may touch |
| `SETUP_MANAGER_TASKS_DB` | `tasks.db` | Where task history is stored |
| `SETUP_MANAGER_ALLOW_SHELL` | `0` | Allow arbitrary shell commands |
| `SETUP_MANAGER_MAX_TASKS` | `4` | Concurrent background tasks |
| `SETUP_MANAGER_OLLAMA_MODEL` | `qwen2.5-coder:7b` | Model used by the AI panel |
| `SETUP_MANAGER_OLLAMA_ALLOWED_MODELS` | see `ollama_client.py` | Which models may be used |

`SETUP_MANAGER_SAFE_ROOT` matters: every path the API is given is resolved and
checked against it, so a request cannot reach outside the projects folder.

## Security

The API listens on localhost only and defends itself against the ways a
browser on the same machine can be abused:

- **Host allow-list** - blocks DNS-rebinding attacks.
- **Per-session token** - every request needs `X-Setup-Token`, so another
  website cannot drive the API.
- **Origin check** - blocks cross-site requests and websockets.
- **Path validation** - paths are resolved (symlinks, junctions, `..`) and
  must land inside the safe root.
- **Security headers and a CSP** on every response.

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
ollama pull qwen2.5-coder:7b
```

The dropdown only offers models that are both allowed by the configuration and
actually installed on the machine.
