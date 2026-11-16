# Security notes

Setup Manager is a local tool. It is meant to run on one Windows PC and
help you set up projects you already have. The defenses below reduce the
chance that a website, a bad path, or a copied command can drive it. They
are a safety net, not a guarantee.

## What it protects

- **Localhost only.** The server binds to `127.0.0.1`. Requests whose
  socket peer is not a loopback address are refused, even if the Host
  header looks local.
- **Session token.** Most API routes require the `X-Setup-Token` header.
  A new random token is created each time the app starts, unless you set
  `SETUP_MANAGER_TOKEN`. The dashboard page receives the token so your
  browser can use the API. Origin alone is never treated as proof of
  identity.
- **Host and Origin checks.** A host allow-list (`127.0.0.1`, `localhost`,
  `[::1]`) blocks DNS-rebinding style requests. Browser requests with an
  Origin header must match the Host they were sent to. The same checks
  apply to the log websocket.
- **No shell by default.** Custom commands are parsed into an argument
  list and run without the system shell. `SETUP_MANAGER_ALLOW_SHELL=1`
  turns shell mode on.
- **Risky command blocking.** The Run box and AI setup suggestions are
  checked for patterns such as download-and-run, hidden or encoded
  PowerShell, start-up persistence, turning off antivirus or the
  firewall, reading saved passwords or keys, wiping disks, and grabbing
  admin rights. A match is refused. This is a pattern check, so a
  harmful command can still be disguised.
- **Safe root.** Project paths are resolved (including `..`, symlinks,
  and junctions) and must land inside the projects root. The root is the
  folder chosen in the dashboard, or `SETUP_MANAGER_SAFE_ROOT`, or a
  `Projects` folder in your user profile. A whole drive, your home
  folder itself, Windows, and Program Files cannot be set as the root.
- **Microsoft Defender.** On Windows, a custom Defender scan can run
  before installs, launches, and some other actions. A detected threat
  blocks the operation. If Defender is unavailable, the operation
  continues unless you set `SETUP_MANAGER_REQUIRE_DEFENDER=1`.

The dashboard also sends standard security headers, including a Content
Security Policy. Page scripts live in `static/dashboard.js`; inline
scripts are not used.

The project's own scripts are not checked in the same way. Install,
Launch, make targets, and setup hooks run the project's code as it is.
Only install or run projects you trust.

## Known limits

- **The source scanner is heuristic.** It looks for known patterns in
  source files. It can miss new or hidden threats, and it can flag
  ordinary code. It does not run the project, does not test exploits, and
  does not audit dependency CVEs. When Defender is clean, a high
  heuristic score is labeled "Review suggested" rather than "CRITICAL."
  Real Defender detections and malware-style rule matches are not softened
  in that way.
- **The session token is readable by programs on this PC.** It is placed
  in the dashboard page so the browser can call the API. Anything already
  running as you on this computer can read that page, the process, or
  (if you set it) the environment variable. The localhost bind and the
  token are meant to stop other websites and other machines, not other
  local programs.
- **Desktop and Downloads are allowed for chosen files.** Project folders
  must still be inside the safe root. A file you pick, such as a disk
  image, may also come from your Desktop or Downloads folder. Treat those
  folders as part of the trusted area for file picks.

## Reporting a problem

If you have access to this repository, open a GitHub issue. Please include
the Setup Manager version, your Python and Windows versions, and the steps
to reproduce the problem.

Do not include the API token, the contents of `run.local.bat`, passwords,
or personal file paths. If the issue is a security weakness, say so in
the title and keep secrets out of the report.
