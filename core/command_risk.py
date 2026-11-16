"""Spot commands that could hurt the computer they run on.

Setup Manager never runs an AI suggestion by itself, but people copy what
the assistant says into the Run box. This check stands between the two.
It looks for the moves malware makes: download-and-run, hidden or encoded
PowerShell, start-up persistence, switching off antivirus or the firewall,
reading saved passwords or keys, wiping disks, and grabbing admin rights.

It is a pattern check, not a guarantee. A command that passes can still
be harmful, so the dashboard still asks before anything runs.
"""
import re
from collections import namedtuple
from typing import Optional

_DOWNLOAD = (
    r"(?:\bcurl\b|\bwget\b|\biwr\b|\birm\b|invoke-webrequest|invoke-restmethod|"
    r"downloadstring|downloaddata|downloadfile|net\.webclient|start-bitstransfer)"
)
_SHELLS = r"(?:(?:sudo\s+)?(?:ba|z|da|k)?sh|python[\d.]*|perl|ruby|node|php|iex|invoke-expression|powershell|pwsh|cmd)\b"
_RUN_SINKS = r"(?:\biex\b|invoke-expression|scriptblock\]::create|invoke-command|\.invoke\(|assembly\]::load|(?:^|[\s;(|])&\s*[(\[$])"
_SERVICES = r"\b(?:windefend|wdfilter|wdnissvc|sense|mpssvc|wscsvc|securityhealthservice)\b"
_WINDOW = r"\s-w(?:in?d?o?w?s?t?y?l?e?)?\s+"

#: The ssh tools themselves may name a key, as long as nothing is chained on.
_SSH_TOOL = r"^(?=.*(?:\bssh-add\b|\bssh-keygen\b|\bssh-copy-id\b|\bssh\s))[^|;&><]*$"
#: Appending a plain PATH line to a shell start-up file is ordinary set-up.
_PATH_LINE = r"^echo\s+export\s+path=[\w$:/.~{}\-]+\s*>>\s*\S*\.(?:bashrc|zshrc|bash_profile|profile)$"

#: ``group`` says what kind of move a rule catches. Apply (a code change,
#: not a command) only uses the high-signal groups in ``CODE_GROUPS``.
Rule = namedtuple("Rule", "group pattern reason unless", defaults=(None,))

RULES = [
    # Download and run
    Rule("download", _DOWNLOAD + r".*\|\s*" + _SHELLS, "downloads code from the internet and runs it straight away"),
    Rule("download", r"(?:\b(?:ba|z)?sh|\bsource|\bpython[\d.]*|\.)\s+<\(\s*(?:curl|wget)", "downloads code from the internet and runs it straight away"),
    Rule("download", r"\$\(\s*(?:curl|wget)\b", "downloads code from the internet and runs it straight away"),
    Rule("download", r"(?=.*download(?:string|data)\b)(?=.*" + _RUN_SINKS + ")", "downloads code from the internet and runs it straight away"),
    Rule("download", r"(?=.*(?:urlopen|urlretrieve|requests\.(?:get|post)|httpx\.(?:get|post)))(?=.*\b(?:exec|eval)\s*\()", "downloads code from the internet and runs it straight away"),
    Rule("download", r"\b(?:iex|invoke-expression)\b|scriptblock\]::create", "runs PowerShell code that is built while it runs"),
    Rule("download", r"\bcertutil(?:\.exe)?\b.*-(?:urlcache|decode|decodehex)\b", "uses certutil to download or unpack a file, a common malware trick"),
    Rule("download", r"\bbitsadmin(?:\.exe)?\b.*/transfer\b", "uses bitsadmin to download a file, a common malware trick"),
    Rule("download", r"\bmshta(?:\.exe)?\b", "runs a script through mshta, a common malware trick"),
    Rule("download", r"\bregsvr32(?:\.exe)?\b.*/i:\s*(?:https?|ftp)", "loads code from the internet through regsvr32"),
    Rule("download", r"\brundll32(?:\.exe)?\b.*(?:javascript:|https?:)", "loads code through rundll32"),
    Rule("download", r"\bbase64\s+(?:-d|--decode)\b.*\|\s*" + _SHELLS, "decodes hidden code and runs it"),
    # Encoded or hidden PowerShell
    Rule("encoded", r"(?:powershell|pwsh)(?:\.exe)?\b.*\s-e(?:c|nc|nco|ncod|ncode|ncoded|ncodedcommand)?\s+[a-z0-9+/=]{8,}", "runs encoded PowerShell that hides what it does"),
    Rule("encoded", r"frombase64string", "decodes hidden code"),
    Rule("encoded", _WINDOW + r"hidden\b|(?:powershell|pwsh)\b.*" + _WINDOW + r"1\b", "runs a hidden window"),
    # Persistence
    Rule("persist", r"\\+currentversion\\+(?:run|runonce|policies\\+explorer\\+run)\b", "makes a program start every time Windows starts"),
    Rule("persist", r"\\+winlogon\\+|image file execution options", "changes how Windows starts programs"),
    Rule("persist", r"\bschtasks(?:\.exe)?\b.*/create\b|register-scheduledtask|new-scheduledtask", "creates a scheduled task that keeps running later"),
    Rule("persist", r"\bsc(?:\.exe)?\s+create\b|\bnew-service\b", "installs a background service"),
    Rule("persist", r"\\+start menu\\+programs\\+startup|shell:startup", "puts a program in the Startup folder"),
    Rule("persist", r"\bwmic\b.*(?:process\s+call\s+create|subscription)", "starts or hides programs through WMI"),
    Rule("startup", r"\bcrontab\b|@reboot", "creates a scheduled job that keeps running later"),
    Rule("startup", r"\bsystemctl\s+enable\b|\blaunchctl\s+(?:load|bootstrap)\b", "makes a program start with the computer"),
    Rule("startup", r">>?\s*\S*\.(?:bashrc|zshrc|bash_profile|profile)\b", "changes your shell start-up file", _PATH_LINE),
    # Antivirus, firewall and logs
    Rule("defense", r"add-mppreference|set-mppreference|disablerealtimemonitoring|exclusionpath|exclusionprocess", "changes Microsoft Defender settings"),
    Rule("defense", r"(?:\bsc(?:\.exe)?\s+(?:stop|config|delete)|\bnet(?:\.exe)?\s+stop|\bstop-service)\b.*" + _SERVICES, "stops or changes the antivirus or firewall service"),
    Rule("defense", r"(?=.*\bset-service\b.*" + _SERVICES + r")(?=.*-startuptype\s+disabled\b)", "stops or changes the antivirus or firewall service"),
    Rule("defense", r"\bnetsh\b.*firewall\b.*\b(?:off|disable|delete)\b", "turns the firewall off or removes its rules"),
    Rule("defense", r"set-netfirewallprofile.*-enabled\s+(?:false|0)|\bufw\s+disable\b|\biptables\s+-f\b|\bsetenforce\s+0\b", "turns the firewall or system protection off"),
    Rule("defense", r"\bwevtutil\s+cl\b|clear-eventlog|\bbcdedit\b", "clears logs or changes how Windows boots"),
    # Passwords, keys and browser data
    Rule("secrets", r"[\\/]\.ssh\b|\bid_(?:rsa|dsa|ecdsa|ed25519)\b", "reads your SSH keys", _SSH_TOOL),
    Rule("secrets", r"[\\/]\.aws[\\/]|\.git-credentials|[\\/]\.netrc\b|/etc/shadow\b", "reads saved passwords or cloud keys"),
    Rule("secrets", r"(?:user data|firefox[\\/]profiles|\.mozilla[\\/]).*(?:login data|local state|cookies|web data)|logins\.json|key4\.db|cookies\.sqlite|wallet\.dat|\\local storage\\leveldb", "reads browser passwords, cookies or wallets"),
    Rule("secrets", r"\bmimikatz\b|\blsass\b|sekurlsa|\breg(?:\.exe)?\s+save\s+hklm\\(?:sam|system|security)\b|\bcmdkey\s+/list\b|\bvaultcmd\b|find-generic-password", "reads Windows or keychain passwords"),
    Rule("secrets", r"\|\s*(?:nc|ncat|netcat|curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b", "sends data from this computer over the network"),
    Rule("secrets", r"\bcurl\b.*(?:--upload-file\b|--data-binary\s+@|\s-f\s+\S*=@|\s-t\s+\S)|-infile\b", "uploads a file from this computer"),
    # Wiping
    Rule("wipe", r"\bformat(?:\.com)?\s+[a-z]:", "formats a drive"),
    Rule("wipe", r"\brm\s+-[a-z]*(?:rf|fr)[a-z]*\s+(?:--no-preserve-root\s+)?(?:/|~|\$home|\*)(?:\s|$|/\*)", "deletes your whole drive or home folder"),
    Rule("wipe", r"\b(?:rd|rmdir|del|erase)\b\s+(?:/\w\s+)*[a-z]:\\\*?(?:\s|$)", "deletes a whole drive"),
    Rule("wipe", r"remove-item\b.*(?:\s[a-z]:\\\*?(?:\s|$)|\$env:(?:userprofile|systemroot|windir))", "deletes a whole drive or your user folder"),
    Rule("wipe", r"\bdd\b.*\bof=/dev/(?:sd|hd|nvme|disk|mmcblk)", "overwrites a disk"),
    Rule("wipe", r"\bmkfs\b|\bdiskpart\b|\bcipher(?:\.exe)?\s+/w|\bshred\b|clear-disk|format-volume|initialize-disk", "wipes or formats a disk"),
    Rule("wipe", r"vssadmin.*delete|wbadmin\s+delete|shadowcopy.*delete", "deletes backups and restore points"),
    Rule("wipe", r":\(\)\s*\{.*\};\s*:", "freezes the computer"),
    # Admin rights and remote shells
    Rule("admin", r"\bnet(?:\.exe)?\s+user\b.*/add\b|\bnet(?:\.exe)?\s+localgroup\s+administrators\b|add-localgroupmember.*administrators", "creates a user or makes one an administrator"),
    Rule("admin", r"\bchmod\s+(?:-r\s+)?(?:[ugoa]*\+s|[2467][0-7]{3})\b|\bchmod\s+(?:-r\s+)?777\s+/(?:\s|$)", "gives a program root powers"),
    Rule("admin", r"/etc/sudoers|\busermod\b.*-a?g\s*(?:sudo|wheel|root|admin)\b", "gives an account admin rights"),
    Rule("admin", r"-verb\s+runas\b|\bfodhelper\b|\beventvwr\b|computerdefaults", "asks for or sneaks admin rights"),
    Rule("admin", r"/dev/tcp/|\b(?:nc|ncat|netcat)\b.*\s-[a-z]*e\b|\bsocat\b.*exec", "opens a remote control connection"),
]

#: The groups worth checking inside source code, where words like
#: "crontab" or "format" turn up for ordinary reasons.
CODE_GROUPS = {"download", "encoded", "persist", "defense", "secrets"}

#: Extra advice for a refusal, keyed by reason.
HINTS = {
    "changes your shell start-up file": (
        "To change it on purpose, open the file in a text editor and add the line yourself."
    ),
}

_COMPILED = [
    (rule.group, re.compile(rule.pattern), rule.reason, re.compile(rule.unless) if rule.unless else None)
    for rule in RULES
]

# --------------------------------------------------------------------------- #
#  Two steps: save a download to a file, then run that file
# --------------------------------------------------------------------------- #
_SEPARATORS = re.compile(r"&&|\|\||[;|&]")
_DOWNLOADER = re.compile(r"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer)\b|downloadfile")
_OUTPUT = re.compile(r"(?:-o|--output|--output-document|-outfile|-destination)\s*=?\s*(\S+)")
_URL = re.compile(r"https?://\S+")
_RUNNER = (
    r"(?:(?:sudo|start|start-process|call|cmd(?:\.exe)? /c|source|\.|invoke-item|msiexec(?:\.exe)? /i)\s+)*"
    r"(?:(?:(?:ba|z|da|k)?sh|python[\d.]*|py|node|perl|ruby|php|powershell(?:\.exe)?|pwsh|wscript|cscript)"
    r"(?:\s+-\S+(?:\s+(?:bypass|unrestricted))?)*\s+)?"
)


def _file_name(token: str) -> str:
    token = token.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    return re.split(r"[\\/]", token)[-1]


def _downloads_then_runs(text: str) -> bool:
    segments = [part.strip() for part in _SEPARATORS.split(text)]
    for index, segment in enumerate(segments):
        if not _DOWNLOADER.search(segment):
            continue
        names = {_file_name(match) for match in _OUTPUT.findall(segment)}
        names |= {_file_name(url) for url in _URL.findall(segment)}
        names.discard("")
        for name in names:
            ran = re.compile(r"^" + _RUNNER + r"(?:\S*[\\/])?" + re.escape(name) + r"(?:\s|$)")
            if any(ran.search(later) for later in segments[index + 1 :]):
                return True
    return False


def _normalise(text: str) -> str:
    """Undo the cheap disguises: caret and backtick escapes, quotes, spacing.

    New lines become ``;`` so a second line still counts as a second command.
    """
    text = (text or "").lower()
    text = re.sub(r"[\^`\"']", "", text)
    text = re.sub(r"\s*[\r\n]+\s*", " ; ", text)
    return re.sub(r"[ \t]+", " ", text).strip(" ;")


def command_risk(command: str, groups=None) -> Optional[str]:
    """Why ``command`` looks dangerous, or None when nothing stood out.

    ``groups`` limits the check to some rule groups (see ``CODE_GROUPS``).
    """
    text = _normalise(command)
    if (groups is None or "download" in groups) and _downloads_then_runs(text):
        return "downloads a file from the internet and then runs it"
    for group, pattern, reason, unless in _COMPILED:
        if groups is not None and group not in groups:
            continue
        if pattern.search(text) and not (unless and unless.search(text)):
            return reason
    return None


_COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*|--|rem\s|::)", re.IGNORECASE)


def code_risk(line: str) -> Optional[str]:
    """``command_risk`` for one line of source code, skipping comments."""
    if _COMMENT.match(line or ""):
        return None
    return command_risk(line, CODE_GROUPS)


def refusal(command: str) -> Optional[str]:
    """A plain message for the user when Setup Manager will not run this."""
    reason = command_risk(command)
    if reason is None:
        return None
    hint = HINTS.get(reason, "If you wrote it yourself and trust it, run it in your own terminal.")
    return (
        f"Setup Manager will not run this command: it {reason}. "
        "If an AI or a project told you to run it, that is a warning sign. "
        + hint
    )
