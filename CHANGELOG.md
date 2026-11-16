# Changelog

## 1.2.0 - 2026-11-16

This is the first tagged release of the current Setup Manager tree. The
October 22, 2026 `main` branch already identified itself as 1.2.0. This
tag adds the work done after that date, including the November 16 review
fixes.

### Review fixes (2026-11-16)

- A path that is too long for the API or the operating system now returns
  HTTP 400 (or 422 for request validation) instead of crashing with HTTP 500.
- AI messages, paths, and other request fields have length limits.
- The task database runs `PRAGMA wal_checkpoint(TRUNCATE)` on startup and
  after old tasks are cleaned up, so `tasks.db-wal` does not keep growing.
- Security scanner tuning:
  - C buffer-overflow rules (`sprintf`, `strcpy`, and similar) apply only
    to C and C++ files, so Go's `fmt.Sprintf` is no longer flagged.
  - Documentation files and folders are skipped for those keyword checks.
  - When Microsoft Defender is clean, a high heuristic score is labeled
    "Review suggested" instead of "CRITICAL."
- The folder-open safety reminder is softer: the scan is optional, and the
  wording explains what is and is not checked.
- The dashboard no longer auto-prompts for browser notification permission.
- Routine custom-command audit lines are logged at info level. Blocked
  risky commands and dropped AI suggestions still log as warnings.

### After the 22 October 2026 main

One commit on `setup-manager-v2-live`,
`Setup Manager v1.2.0 live copy with review fixes (2026-11-16)`, brings
the live app onto git. Compared with `origin/main` that is 61 files
changed (about 11,800 lines added and 560 removed), including a new
`LICENSE` and these additions:

- `core/command_risk.py`: pattern checks that block risky Run-box commands
  and AI setup suggestions.
- `core/file_watch.py`: an optional watch for unexpected new programs in a
  project folder.
- `core/guide.py`: next-step guidance from the project's real steps.
- `core/language_templates.py`: language templates for detection and setup.
- `core/privacy.py`: a local check for known tracking and remote-control
  program names.
- `core/project_check.py` and `scripts/check_project.py`: missing files and
  safe, opt-in package downloads from official registries.
- `core/settings.py`: dashboard settings, including the projects root.
- `core/where.py` and `scripts/where.py`: find a program folder by name.
- `core/contained.py`: read project files only from inside a chosen root.
- `doctor/learning.py`: the doctor can record a fix after it is verified.
- `scripts/init_project.py`: start a new project from a language template.
- `static/dashboard.js`: the dashboard script moved out of the HTML page
  so a strict Content Security Policy can be used.
- Tests for the new modules, AI safety, scanner tuning, and the review
  fixes.

The dashboard, security layer, doctor, and Ollama client were also updated
in that same live copy.
