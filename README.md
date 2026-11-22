# Setup Manager

*A simple setup tool made by a simple man and AI to help the ones who want to learn but don't know where to start.*

**Setup Manager** helps you install and run programs on Windows more safely. It checks files before running them, stays on your own computer, and never sends your stuff to the cloud. It's free, MIT licensed, and yours to use, change, and share. If it helps you, pass it on.

Setup Manager helps you set up and run development projects on Windows. It
opens a dashboard in your browser, can detect common project types, install
what a project needs (after you approve the commands), scan a folder for
known warning signs, and explain why a build failed.

It supports Python, Node.js, C# (.NET), Ruby, Make, Git and Docker, plus
common extras such as linters, formatters, type checkers and test runners.
An optional AI panel can talk to a local Ollama model on your PC. Nothing
is sent to a cloud AI service.

## Features

- A web dashboard on this PC only, plus a terminal menu as a backup
- Detects a project's language and missing tools or packages
- Shows the exact install or repair commands and waits for your approval
- A security scan that looks for known risky patterns, plus an optional
  Microsoft Defender scan
- Blocks risky commands in the Run box and in AI suggestions
- A "doctor" that checks this PC and known build errors
- Optional local AI help through Ollama
- Optional file watch for unexpected new programs in a project folder

## Requirements

- Windows
- Python 3, tested with Python 3.13
- Optional: [Ollama](https://ollama.com) if you want the AI panel

## Install

1. Clone the repository, or download the zip from GitHub and unpack it.
2. Open the folder and run `run.bat`.

On the first run, `run.bat` creates a virtual environment in `venv\`,
installs the packages from `requirements.txt`, and starts the dashboard.
Later runs reuse that environment.

To start the terminal menu instead of the dashboard:

```bat
venv\Scripts\python.exe setup_manager.py
```

You can also put a shortcut on your Desktop with `create_shortcut.py`.

## First run

Setup Manager keeps your projects in a `Projects` folder inside your user
folder, for example `C:\Users\You\Projects`. The first time you start it,
that folder is created if it is not there yet. The dashboard, `run.bat`
and the desktop shortcut all use this same folder.

It will not use your whole home folder, and it will not use the folder
the zip was unpacked in (Downloads or Desktop). The `run.bat` window
prints the folder it is using.

To use a different folder:

1. Create `run.local.bat` next to `run.bat` (this file stays on your PC), or
2. In the dashboard, paste the folder into the path box and click **Set as projects root**.

```bat
@echo off
if not defined SETUP_MANAGER_SAFE_ROOT set SETUP_MANAGER_SAFE_ROOT=C:\Users\You\SomeFolder
```

If that folder is missing, or it is one Setup Manager is not allowed to use,
it says so and keeps using `Projects` so the program still opens.

The dashboard prefers [http://127.0.0.1:8010](http://127.0.0.1:8010) and
only listens on this computer. If 8010 is busy, it uses the next free port
in the range 8010-8099 and says so in the `run.bat` window. If Setup
Manager is already running, a second start opens the one that is already
up instead of starting another.

Pick a project folder and use the dashboard to scan, install, or launch.
Installs and repairs always show the commands first and wait for Approve.

If you want the AI panel, start Ollama and pull a model, for example:

```bat
ollama pull qwen2.5-coder:3b
```

The AI pill at the top of the dashboard says whether it is ready. If Ollama
is not running, Setup Manager still works; the pill will say
`AI offline - start Ollama`. Click the pill to check again.

## Projects folder (safe root)

Setup Manager will only open project folders inside one safe folder. The
shortcut, `run.bat` and the dashboard agree on which folder that is. The
default is `Projects` in your user profile, and it is created if needed.
You can change it in the dashboard (**Set as projects root**), or with the
`SETUP_MANAGER_SAFE_ROOT` environment variable.

`run.local.bat` is the local-only place to set that variable. It is listed
in `.gitignore`, so it is not committed or uploaded. `run.bat` calls it
when the file exists. Example `run.local.bat`:

```bat
@echo off
REM Personal settings for this PC. Not shared (see .gitignore).
if not defined SETUP_MANAGER_SAFE_ROOT set SETUP_MANAGER_SAFE_ROOT=C:\Users\You\Projects
```

A whole drive, your home folder itself, Downloads, Desktop, Windows, and
Program Files are refused as the projects root.

## Tests

```bat
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest -q
```

## Security

In short:

- The server binds to `127.0.0.1` and refuses connections from other machines
- API requests need a per-session token (`X-Setup-Token`)
- Host and Origin headers are checked
- Commands are not run through the system shell unless you turn that on
- Risky commands are blocked
- Project paths must stay inside the safe root
- Microsoft Defender can scan a folder before installs or launches

This is a safety net, not a guarantee. See [docs/SECURITY-NOTES.md](docs/SECURITY-NOTES.md)
for the model, its limits, and how to report a problem.

If something goes wrong, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
Changes in this release are listed in [CHANGELOG.md](CHANGELOG.md).

## Configuration

These are the main environment variables. You can set them in
`run.local.bat`.

| Variable | Default | Meaning |
|---|---|---|
| `SETUP_MANAGER_HOST` | `127.0.0.1` | Interface the server binds to |
| `SETUP_MANAGER_PORT` | unset | Pin the server to this port. Unset, it takes the first free port in 8010-8099 |
| `SETUP_MANAGER_PORT_RANGE` | `8010-8099` | Ports the dashboard may use, as `start-end` |
| `SETUP_MANAGER_TOKEN` | random per run | API token, sent as `X-Setup-Token` |
| `SETUP_MANAGER_SAFE_ROOT` | `Projects` in your user profile | The only folder the API may open as a project |
| `SETUP_MANAGER_ALLOW_SHELL` | `0` | Allow arbitrary shell commands |
| `SETUP_MANAGER_OLLAMA_MODEL` | `qwen2.5-coder:3b` when started from `run.bat` | Model used by the AI panel |
| `SETUP_MANAGER_NOTIFY` | `0` | `1` turns on desktop toasts when a task finishes |

## License

Setup Manager is free software under the [MIT License](LICENSE). You can use,
copy, change, and share it, including in your own projects, at no cost.

Copyright (c) 2026 E. Guardado.

This project was developed by E. Guardado with assistance from Grok Bot, and
is shared so other people can set up their own tools the same way.

## Questions or ideas?

Open an [Issue](https://github.com/luckt707-maker/Setup-manager/issues) on this repo.

## Dedication

In memory of JPP, who gave me the strength and the push to go on, and for his mom, Marina Hernandez.

Thanks to Rock, for being a good critic. I couldn't have done it without you.
