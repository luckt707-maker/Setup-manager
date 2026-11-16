from pathlib import Path
from typing import Any, Dict, List, Tuple
import ast
import os
import re
import base64


class SecurityScanner:
    """Perform bounded, read-only checks for common risky source patterns,
    including malware indicators, exploit patterns, trojans, and safety issues.

    Categories:
      - MALWARE: Known malware signatures, droppers, packers
      - REVERSE_SHELL: Reverse/bind shell patterns
      - CRYPTO_MINER: Cryptocurrency mining indicators
      - DATA_EXFIL: Data exfiltration patterns
      - BACKDOOR: Trojan-style backdoors and persistence
      - OBFUSCATION: Code obfuscation techniques
      - EXPLOIT: Known exploit patterns and buffer overflows
      - DANGEROUS_CMD: Dangerous system command usage
      - CREDENTIAL: Hardcoded credentials and secrets
      - INJECTION: Code/command injection vulnerabilities
      - FILE_RISK: Dangerous file operations
    """

    SKIP_DIRS = {
        '.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__',
        '.tox', '.mypy_cache', '.pytest_cache', 'build', 'dist', 'vendor',
    }
    SOURCE_EXTENSIONS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.cs', '.go', '.rb', '.php', '.sh', '.bash', '.bat', '.ps1', '.psm1', '.vbs', '.wsf', '.pl', '.pm', '.java', '.kt', '.swift', '.rs', '.c', '.cpp', '.h', '.hpp'}
    MAX_DEPTH = 6
    MAX_FILES = 1000
    MAX_FILE_BYTES = 100 * 1024
    # C buffer/memory rules only belong in C/C++ sources. Go's fmt.Sprintf,
    # and similar helpers in other languages, are not the unsafe C functions.
    C_LIKE_EXTENSIONS = {'.c', '.cc', '.cxx', '.cpp', '.h', '.hpp', '.hh', '.hxx'}
    C_MEMORY_RULES = frozenset({'EXP001', 'EXP002', 'EXP008', 'EXP009'})
    DOC_DIR_NAMES = frozenset({'docs', 'doc', 'documentation'})
    DOC_EXTENSIONS = frozenset({'.md', '.rst', '.txt', '.adoc'})
    DOC_STEMS = frozenset({
        'readme', 'changelog', 'changes', 'license', 'copying',
        'authors', 'contributing', 'security', 'code_of_conduct', 'notice',
    })
    EMERGENCY_CATEGORIES = frozenset({'MAL', 'RS', 'BD'})

    # ----------------------------------------------------------------------- #
    #  Credential & secret patterns
    # ----------------------------------------------------------------------- #
    SECRET_PATTERN = re.compile(
        r'''(?i)\b(api[_-]?key|client[_-]?secret|password|access[_-]?token|secret[_-]?key|private[_-]?key)\b\s*[:=]\s*["']([^"'\s]{8,})["']'''
    )
    PRIVATE_KEY_PATTERN = re.compile(
        r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----'
        r'|-----BEGIN CERTIFICATE-----'
    )
    AWS_KEY_PATTERN = re.compile(r'(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}')
    GENERIC_TOKEN_PATTERN = re.compile(
        r'(?i)(?:bearer|token|auth)\s*[:=]\s*["\']?[a-zA-Z0-9_\-]{20,}'
    )

    # ----------------------------------------------------------------------- #
    #  Malware indicators
    # ----------------------------------------------------------------------- #
    MALWARE_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Known dropper/downloader patterns
        (re.compile(r'(?i)(?:download|fetch|urlopen|requests\.\w+)\s*\([^)]*(?:exe|bat|cmd|ps1|vbs|scr|js|wsf)'),
         'MAL001', 'critical', 'Code downloads and executes files — possible dropper behavior.'),
        # Process injection patterns
        (re.compile(r'(?i)(?:CreateRemoteThread|NtCreateThreadEx|RtlCreateUserThread|VirtualAllocEx|WriteProcessMemory)\s*\('),
         'MAL002', 'critical', 'Process injection API detected — common in malware for code injection.'),
        # Registry persistence (Windows)
        (re.compile(r'(?i)(?:RegSetKeyValue|RegSetValueEx)\s*\([^)]*(?:Run|RunOnce|CurrentVersion)'),
         'MAL003', 'high', 'Registry modification for persistence — common trojan technique.'),
        # Fileless malware indicators
        (re.compile(r'(?i)(?:Invoke-Expression|IEX|Invoke-WebRequest|Invoke-RestMethod)\s*\('),
         'MAL004', 'high', 'PowerShell fileless execution pattern — common in malware chains.'),
        # Mutex creation for single-instance malware
        (re.compile(r'(?i)(?:CreateMutex|OpenMutex)\s*\([^)]*(?:Global\\|Global\\|Local\\)'),
         'MAL005', 'medium', 'Mutex creation for single-instance enforcement — common in malware.'),
        # Keylogger patterns
        (re.compile(r'(?i)(?:SetWindowsHookEx|GetAsyncKeyState|GetKeyState|LowLevelKeyboard|keylog)\s*\('),
         'MAL006', 'critical', 'Keylogger API detected — captures keyboard input.'),
        # Screenshot/screen capture malware
        (re.compile(r'(?i)(?:BitBlt|GetDC|CreateCompatibleBitmap|screen.*shot|capture.*screen)\s*\('),
         'MAL007', 'high', 'Screen capture API detected — possible surveillance malware.'),
        # Anti-debugging techniques
        (re.compile(r'(?i)(?:IsDebuggerPresent|CheckRemoteDebuggerPresent|NtQueryInformationProcess)\s*\('),
         'MAL008', 'medium', 'Anti-debugging check detected — malware may hide behavior when debugged.'),
        # VM/sandbox detection
        (re.compile(r'(?i)(?:sandbox|vmware|virtualbox|vbox|hyper-v|xen|kvm|qemu|test.*machine)'),
         'MAL009', 'medium', 'VM/sandbox detection — malware may behave differently in analysis environments.'),
        # Crypter/packer indicators
        (re.compile(r'(?i)(?:xor.*decrypt|rc4.*decrypt|aes.*decrypt|decrypt.*payload|unpack.*code)'),
         'MAL010', 'high', 'Encryption/decryption of embedded payload — possible crypter or packer.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Reverse shell patterns
    # ----------------------------------------------------------------------- #
    REVERSE_SHELL_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Bash reverse shell
        (re.compile(r'/bin/(?:ba)?sh\s+-i\s+>&\s*/dev/tcp/'),
         'RS001', 'critical', 'Bash reverse shell — connects back to attacker-controlled host.'),
        # Netcat reverse shell
        (re.compile(r'(?i)nc(?:at)?\s+(-[el]|-c|-e)\s*(?:/bin/)?(?:sh|bash|cmd)'),
         'RS002', 'critical', 'Netcat reverse shell — provides remote shell access.'),
        # Python reverse shell
        (re.compile(r'(?i)socket\s*\.\s*socket.*?connect\s*\([^)]*\).*?(?:exec|system|popen)'),
         'RS003', 'critical', 'Python socket reverse shell pattern — remote code execution.'),
        # PHP reverse shell
        (re.compile(r'(?i)fsockopen\s*\([^)]*\).*?popen\s*\(|proc_open\s*\([^)]*(?:sh|bash|cmd)'),
         'RS004', 'critical', 'PHP reverse shell — opens remote shell connection.'),
        # PowerShell reverse shell
        (re.compile(r'(?i)(?:New-Object|System\.Net\.TcpClient).*?(?:StreamReader|StreamWriter)'),
         'RS005', 'critical', 'PowerShell TCP reverse shell — remote command execution.'),
        # Perl reverse shell
        (re.compile(r'(?i)socket\s+SOCK.*?INET.*?connect.*?exec\s*["\'](?:sh|bash|cmd)'),
         'RS006', 'critical', 'Perl reverse shell — remote command execution.'),
        # Ruby reverse shell
        (re.compile(r'(?i)require\s+["\']socket["\'].*?TCPSocket\.open'),
         'RS007', 'critical', 'Ruby reverse shell — remote command execution.'),
        # Java reverse shell
        (re.compile(r'(?i)Runtime\.getRuntime\(\)\.exec\s*\([^)]*(?:bash|sh|cmd|powershell)'),
         'RS008', 'critical', 'Java reverse shell — executes system shell commands.'),
        # Go reverse shell
        (re.compile(r'(?i)(?:net\.Dial|net\.TCPConn).*?(?:exec\.Command|cmd\.Run)'),
         'RS009', 'critical', 'Go reverse shell — remote command execution.'),
        # Bind shell (listening socket)
        (re.compile(r'(?i)(?:socket|listen|bind)\s*\([^)]*\d+\).*?(?:accept|exec|system)'),
         'RS010', 'critical', 'Bind shell pattern — listens for incoming connections and executes commands.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Cryptocurrency miner patterns
    # ----------------------------------------------------------------------- #
    CRYPTO_MINER_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Known mining pools
        (re.compile(r'(?i)(?:stratum\+tcp|stratum+tcp| mining.*pool|pool\.(?:minexmr|coinhive|cryptonight|nicehash|supportxmr))'),
         'CM001', 'critical', 'Cryptocurrency mining pool connection detected.'),
        # Mining library references
        (re.compile(r'(?i)(?:coinhive|cryptonight|xmrig|cpuminer|bfgminer|cgminer|minerd|ethminer|ccminer)'),
         'CM002', 'critical', 'Known cryptocurrency mining software reference.'),
        # Web miner script injection
        (re.compile(r'(?i)(?:coinhive|cryptonight|web.*miner)\s*\.\s*(?:new|start|create)'),
         'CM003', 'critical', 'JavaScript web miner instantiation — mines crypto in browser.'),
        # High CPU usage patterns
        (re.compile(r'(?i)(?:while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;\s*\))\s*\{?\s*(?:[a-z]\s*=\s*[a-z].*[+\-*\/]|hash|compute|solve|mine)'),
         'CM004', 'high', 'Infinite compute loop — possible crypto miner or CPU abuse.'),
        # GPU mining APIs
        (re.compile(r'(?i)(?:cuda|opencl|nvml|rocm).*?(?:mine|hash|work|solve|nonce)'),
         'CM005', 'high', 'GPU mining API usage — cryptocurrency mining on GPU.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Data exfiltration patterns
    # ----------------------------------------------------------------------- #
    DATA_EXFIL_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # DNS exfiltration
        (re.compile(r'(?i)(?:dns.*exfil|exfil.*dns|resolve.*encode|encode.*dns|subdomain.*data)'),
         'DE001', 'critical', 'DNS-based data exfiltration pattern detected.'),
        # Stealth HTTP POST of data
        (re.compile(r'(?i)(?:requests\.\w+|fetch|axios|http\.post)\s*\([^)]*(?:password|secret|token|credential|ssn|credit)'),
         'DE002', 'high', 'Sensitive data being sent over HTTP — possible data exfiltration.'),
        # Clipboard data theft
        (re.compile(r'(?i)(?:clipboard|GetClipboardData|NSPasteboard|XClip).*?(?:send|post|upload|exfil)'),
         'DE003', 'high', 'Clipboard data exfiltration — stealing copied content.'),
        # Browser data theft
        (re.compile(r'(?i)(?:chrome.*(?:Login|Password|Cookie)|firefox.*(?:key3|logins|cookies)|safari.*(?:Bookmarks|History))'),
         'DE004', 'critical', 'Browser credential/data theft — accessing saved passwords or cookies.'),
        # File enumeration for exfiltration
        (re.compile(r'(?i)(?:glob|globall|findall|listdir).*?(?:\.pdf|\.docx?|\.xlsx?|\.pptx?|\.csv|\.sql|\.db)'),
         'DE005', 'medium', 'File enumeration targeting sensitive document types — possible data staging.'),
        # Compress and send
        (re.compile(r'(?i)(?:tarfile|zipfile|gzip|7z).*?(?:send|post|upload|write.*socket)'),
         'DE006', 'high', 'Compressing and sending data — possible data exfiltration staging.'),
        # Base64 encode and send
        (re.compile(r'(?i)base64.*?(?:encode|b64encode).*?(?:send|post|upload|write)'),
         'DE007', 'medium', 'Base64 encoding before sending — common exfiltration technique to evade filters.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Backdoor & persistence patterns
    # ----------------------------------------------------------------------- #
    BACKDOOR_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Hidden admin accounts
        (re.compile(r'(?i)(?:useradd|net user|CreateUser|Add-LocalUser).*?(?:admin|root|backdoor|hidden)'),
         'BD001', 'critical', 'Creating hidden admin account — backdoor persistence.'),
        # Cron/scheduled task persistence.
        # "at" has to be the scheduler command (`at 14:00`), not the word in "that" or "at social".
        (re.compile(
            r'(?i)(?:\bcrontab\b|\bschtasks(?:\.exe)?\b|\bRegister-ScheduledTask\b|'
            r'\bNew-ScheduledTask\b|\bSchedule\.Task\b|'
            r'(?<![A-Za-z0-9_])at(?:\.exe)?\s+\d{1,2}:\d{2}\b)'
        ),
         'BD002', 'high', 'Scheduled task/cron persistence — backdoor may survive reboots.'),
        # Web shell patterns
        (re.compile(r'(?i)(?:eval\s*\(\s*\$_(POST|GET|REQUEST|COOKIE)|system\s*\(\s*\$_(POST|GET|REQUEST))'),
         'BD003', 'critical', 'PHP web shell — executes arbitrary commands via HTTP parameters.'),
        # Hidden network listeners
        (re.compile(r'(?i)(?:socket|listen|bind)\s*\([^)]*(?:31337|4444|1337|6666|9999|8888|7777)'),
         'BD004', 'high', 'Listening on suspicious port — possible backdoor listener.'),
        # Disable security software
        (re.compile(r'(?i)(?:disable.*antivirus|stop.*defender|kill.*security|uninstall.*firewall|Set-MpPreference.*Disable)'),
         'BD005', 'critical', 'Disabling security software — backdoor evading detection.'),
        # Rootkit indicators
        (re.compile(r'(?i)(?:rootkit|hide.*process|hook.*syscall|inline.*hook|ssdt|idt)'),
         'BD006', 'critical', 'Rootkit technique detected — hiding processes or hooking system calls.'),
        # SSH key injection
        (re.compile(r'(?i)(?:authorized_keys|\.ssh).*?(?:write|append|add)'),
         'BD007', 'high', 'SSH key injection — backdoor access via SSH.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Obfuscation patterns
    # ----------------------------------------------------------------------- #
    OBFUSCATION_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Heavy base64 encoding
        (re.compile(r'(?i)(?:base64|b64decode|atob|Buffer.*from.*base64)\s*\([^)]{50,}'),
         'OBF001', 'high', 'Large base64-encoded string — possible obfuscated payload.'),
        # Hex string obfuscation
        (re.compile(r'(?:\\x[0-9a-fA-F]{2}){20,}'),
         'OBF002', 'high', 'Long hex-encoded string — possible obfuscated command or payload.'),
        # Character code obfuscation
        (re.compile(r'(?i)(?:String\.fromCharCode|chr\s*\(\s*\d+\s*\))\s*\+.*?(?:String\.fromCharCode|chr\s*\(\s*\d+\s*\))'),
         'OBF003', 'high', 'Character code concatenation — obfuscated string construction.'),
        # Eval with encoded content
        (re.compile(r'(?i)eval\s*\(\s*(?:atob|b64decode|decode|uncompress|gzinflate)\s*\('),
         'OBF004', 'critical', 'Eval of decoded/compressed content — classic obfuscated malware pattern.'),
        # String split/join obfuscation
        (re.compile(r'(?i)(?:split|join)\s*\([^)]*\)\s*\.\s*(?:split|join)\s*\([^)]*\)\s*\.\s*(?:split|join)'),
         'OBF005', 'medium', 'Chained split/join operations — string obfuscation technique.'),
        # Rot13 or custom cipher. A dotted .decode() is bytes/str decoding, not a cipher.
        (re.compile(r'(?i)(?<!\.)\b(?:rot13|caesar|cipher|decrypt|decode)\s*\('),
         'OBF006', 'medium', 'Custom cipher/decode function — possible obfuscation layer.'),
        # Variable name obfuscation (single char vars in long expressions)
        (re.compile(r'(?:[a-z]\s*=\s*[a-z]\s*[+\-*\/]\s*[a-z]){5,}', re.I),
         'OBF007', 'medium', 'Single-character variable chain — possible obfuscated logic.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Exploit patterns
    # ----------------------------------------------------------------------- #
    EXPLOIT_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Buffer overflow indicators (C/C++)
        (re.compile(r'(?i)(?:gets|strcpy|strcat|sprintf|vsprintf)\s*\('),
         'EXP001', 'high', 'Unsafe C string function — buffer overflow vulnerability.'),
        # Format string vulnerability
        (re.compile(r'(?:printf|sprintf|fprintf|snprintf)\s*\(\s*(?:[^,]*,)?\s*(?:user|input|argv|buf|str|data)'),
         'EXP002', 'high', 'Format string vulnerability — attacker-controlled format string.'),
        # SQL injection patterns
        (re.compile(r'(?i)(?:execute|cursor|query)\s*\(\s*(?:f["\']|format|%\s*|\.format|`\s*\{|\+\s*\w+\s*\+|concat)'),
         'EXP003', 'critical', 'SQL query with string formatting — SQL injection vulnerability.'),
        # Command injection
        (re.compile(r'(?i)(?:system|exec|popen|passthru|shell_exec|proc_open)\s*\(\s*(?:\$_|GET|POST|REQUEST|input|argv|user)'),
         'EXP004', 'critical', 'Command injection — user input passed directly to shell command.'),
        # Path traversal
        (re.compile(r'(?i)(?:open|read|include|require)\s*\([^)]*(?:\.\./|\.\.\\\\|\\x2e\\x2e|\\x252e)'),
         'EXP005', 'high', 'Path traversal pattern — accessing files outside intended directory.'),
        # XSS patterns
        (re.compile(r'(?i)(?:document\.write|innerHTML|outerHTML)\s*=\s*(?:window\.location|document\.cookie|location\.href|event)'),
         'EXP006', 'high', 'Cross-site scripting (XSS) — unsanitized data rendered as HTML.'),
        # Deserialization vulnerabilities
        (re.compile(r'(?i)(?:unserialize|ObjectInputStream|yaml\.load|marshal\.loads)\s*\([^)]*(?:\$_|GET|POST|REQUEST|input|request)'),
         'EXP007', 'critical', 'Deserialization of untrusted data — remote code execution risk.'),
        # Integer overflow
        (re.compile(r'(?i)(?:malloc|calloc|realloc)\s*\(\s*(?:\w+\s*\*\s*\w+|\w+\s*\+\s*\w+)'),
         'EXP008', 'medium', 'Integer arithmetic in memory allocation — possible integer overflow.'),
        # Use-after-free indicators
        (re.compile(r'(?i)(?:free\s*\([^)]*\).*?\w+\s*[\->]|delete\s+\w+.*?\w+\s*[\->])'),
         'EXP009', 'high', 'Possible use-after-free — accessing memory after deallocation.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Dangerous command patterns
    # ----------------------------------------------------------------------- #
    DANGEROUS_CMD_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # Destructive commands
        (re.compile(r'(?i)(?:rm\s+-rf\s+/|format\s+[a-z]:|mkfs|dd\s+if=|diskpart)'),
         'DC001', 'critical', 'Destructive system command — data loss or system damage risk.'),
        # Privilege escalation
        (re.compile(r'(?i)(?:sudo|su\s+root|runas|privilege.*escalat|setuid|setgid|chmod\s+([47]\d{2}|[0-7][47]\d))'),
         'DC002', 'high', 'Privilege escalation attempt — gaining elevated system access.'),
        # Firewall manipulation
        (re.compile(r'(?i)(?:iptables|ufw|firewall-cmd|netsh.*firewall|Set-NetFirewallRule).*?(?:allow|accept|disable|flush)'),
         'DC003', 'high', 'Firewall manipulation — disabling network security controls.'),
        # Network sniffing
        (re.compile(r'(?i)(?:tcpdump|wireshark|tshark|dumpcap|npcap|pcap_open_live)'),
         'DC004', 'medium', 'Network packet capture — possible network sniffing.'),
        # Password cracking
        (re.compile(r'(?i)(?:hashcat|john|hydra|medusa|ncrack|aircrack|hashcat)'),
         'DC005', 'high', 'Password cracking tool reference — brute force attack capability.'),
        # System information gathering (recon)
        (re.compile(r'(?i)(?:nmap|masscan|zmap|arp-scan|fping).*?(?:scan|discover|enumerate)'),
         'DC006', 'medium', 'Network reconnaissance tool — scanning and enumeration.'),
    ]

    # ----------------------------------------------------------------------- #
    #  File risk patterns
    # ----------------------------------------------------------------------- #
    FILE_RISK_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
        # World-writable files
        (re.compile(r'(?i)(?:chmod\s+(777|o\+w)|FileMode.*0777)'),
         'FR001', 'high', 'World-writable file permission — any user can modify the file.'),
        # Sensitive file access
        (re.compile(r'(?i)(?:/etc/(?:passwd|shadow|sudoers|hosts)|C:\\\\Windows\\\\(?:System32|win\.ini|system\.ini))'),
         'FR002', 'high', 'Accessing sensitive system files — possible privilege escalation.'),
        # Writing to system directories
        (re.compile(r'(?i)(?:write|create|open.*w).*?(?:/usr/bin|/usr/sbin|/etc/|C:\\\\Windows\\\\System32)'),
         'FR003', 'critical', 'Writing to system directory — possible system compromise.'),
        # Hidden file creation. ".." is a normal relative path, not a hidden file.
        (re.compile(
            r'(?i)(?:\battrib(?:\.exe)?\s+\+h\b|\bchattr\s+\+i\b|\bFILE_ATTRIBUTE_HIDDEN\b|'
            r'(?<![\w.])\.hidden\b)'
        ),
         'FR004', 'medium', 'Creating hidden files or setting hidden attributes — hiding malicious files.'),
    ]

    # ----------------------------------------------------------------------- #
    #  JavaScript-specific rules (original + extended)
    # ----------------------------------------------------------------------- #
    JS_RULES: List[Tuple[re.Pattern, str, str, str]] = [
        (re.compile(r'\beval\s*\('), 'SEC001', 'medium', 'Dynamic code execution with eval().'),
        (re.compile(r'\bnew\s+Function\s*\('), 'SEC002', 'medium', 'Dynamic code construction with Function().'),
        (re.compile(r'\bchild_process\s*\.\s*exec(?:Sync)?\s*\('), 'SEC003', 'high', 'Shell command execution; validate all inputs.'),
        (re.compile(r'dangerouslySetInnerHTML'), 'SEC004', 'medium', 'Raw HTML insertion can enable cross-site scripting.'),
        # Node.js prototype pollution
        (re.compile(r'(?i)(?:__proto__|constructor\.prototype)\s*\['), 'SEC010', 'high', 'Prototype pollution vulnerability — object property injection.'),
        # Node.js insecure deserialization
        (re.compile(r'\bnode-serialize\b|\bserialize\-javascript\b'), 'SEC011', 'high', 'Known vulnerable Node.js serialization library.'),
    ]

    # ----------------------------------------------------------------------- #
    #  Suspicious file names
    # ----------------------------------------------------------------------- #
    SUSPICIOUS_FILE_NAMES = {
        'keylogger', 'keylog', 'keyrecorder', 'screenrec', 'screencap',
        'backdoor', 'rootkit', 'trojan', 'rat', 'remoteaccess',
        'miner', 'mining', 'cryptominer', 'xmrig', 'coinhive',
        'exploit', 'payload', 'shellcode', 'reverse_shell', 'bind_shell',
        'webshell', 'c2', 'command_control', 'botnet', 'bot',
        'ransom', 'ransomware', 'encryptor', 'cryptolocker',
        'stealer', 'grabber', 'sniffer', 'logger',
        'dropper', 'downloader', 'loader', 'injector',
        'privesc', 'escalate', 'exploitme', 'vuln',
    }

    # ----------------------------------------------------------------------- #
    #  Suspicious file extensions
    # ----------------------------------------------------------------------- #
    SUSPICIOUS_EXTENSIONS = {
        '.exe', '.dll', '.bat', '.cmd', '.ps1', '.psm1', '.vbs', '.wsf',
        '.scr', '.pif', '.com', '.cpl', '.msi', '.jar', '.hta',
    }

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = SecurityScanner._call_name(node.value)
            return f'{prefix}.{node.attr}' if prefix else node.attr
        return ''

    # ----------------------------------------------------------------------- #
    #  Severity scoring
    # ----------------------------------------------------------------------- #
    SEVERITY_SCORE = {
        'critical': 10,
        'high': 7,
        'medium': 4,
        'low': 1,
    }

    # Category labels for display
    CATEGORY_LABEL = {
        'MAL': '🦠 Malware',
        'RS': '🔌 Reverse Shell',
        'CM': '⛏️ Crypto Miner',
        'DE': '📤 Data Exfiltration',
        'BD': '🚪 Backdoor',
        'OBF': '🎭 Obfuscation',
        'EXP': '💥 Exploit',
        'DC': '⚠️ Dangerous Command',
        'SEC': '🔐 Credential/Injection',
        'FR': '📁 File Risk',
    }

    @classmethod
    def _python_findings(cls, source: str, relative_path: str) -> List[Dict[str, str]]:
        """Analyze Python source code using AST for precise detection."""
        findings = []
        try:
            tree = ast.parse(source, filename=relative_path)
        except SyntaxError:
            return findings

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = cls._call_name(node.func)
            rule = None
            severity = 'medium'
            message = ''
            fixable = False
            fix_hint = ''

            if call_name in {'eval', 'exec'}:
                rule, message = 'SEC001', 'Dynamic code execution with eval()/exec().' 
            elif call_name == 'os.system':
                dynamic_command = not node.args or not isinstance(node.args[0], ast.Constant)
                if dynamic_command:
                    rule, severity, message = 'SEC003', 'high', 'Shell command execution uses a dynamic value; validate all inputs.'
            elif call_name in {'pickle.load', 'pickle.loads', 'dill.load', 'dill.loads'}:
                rule, severity, message = 'SEC005', 'high', 'Deserializing untrusted data can execute arbitrary code.'
            elif call_name == 'yaml.load' and not any(
                keyword.arg == 'Loader'
                and isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr in {'SafeLoader', 'CSafeLoader'}
                for keyword in node.keywords
            ):
                rule, severity, message = 'SEC006', 'high', 'yaml.load() may construct unsafe Python objects; prefer safe_load().'
            elif call_name.startswith('subprocess.') and any(
                keyword.arg == 'shell'
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                rule, severity, message = 'SEC007', 'high', 'subprocess with shell=True can enable command injection.'
                arg = node.args[0] if node.args else None
                command_is_list = isinstance(arg, (ast.List, ast.Tuple)) and all(
                    isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    for elt in arg.elts
                )
                # A list does not need the shell. A string does, and rewriting its quotes can change what runs.
                fixable = command_is_list
                fix_hint = (
                    'The command is already a list, so this can be switched to shell=False.'
                    if command_is_list else
                    'This passes a shell command string. Setup Manager will not rewrite it, because changing the quoting can change what runs.'
                )

            if rule:
                findings.append({
                    'file': relative_path,
                    'line': str(node.lineno),
                    'rule': rule,
                    'severity': severity,
                    'message': message,
                    'fixable': fixable,
                    'fix_hint': fix_hint,
                })
        return findings

    @classmethod
    def _is_doc_path(cls, relative_path: str) -> bool:
        """README, docs folders, and markup files are not treated as exploit sources."""
        path = Path(relative_path)
        if any(part.lower() in cls.DOC_DIR_NAMES for part in path.parts[:-1]):
            return True
        if path.suffix.lower() in cls.DOC_EXTENSIONS:
            return True
        stem = path.stem.lower()
        return stem in cls.DOC_STEMS or stem.startswith("readme")

    @classmethod
    def _check_patterns(
        cls, source: str, relative_path: str,
        patterns: List[Tuple[re.Pattern, str, str, str]]
    ) -> List[Dict[str, str]]:
        """Check source lines against a list of regex patterns."""
        findings = []
        suffix = Path(relative_path).suffix.lower()
        for line_number, line in enumerate(source.splitlines(), 1):
            for pattern, rule, severity, message in patterns:
                if rule in cls.C_MEMORY_RULES and suffix not in cls.C_LIKE_EXTENSIONS:
                    continue
                if pattern.search(line):
                    findings.append({
                        'file': relative_path,
                        'line': str(line_number),
                        'rule': rule,
                        'severity': severity,
                        'message': message,
                    })
        return findings

    @classmethod
    def _check_credentials(cls, source: str, relative_path: str) -> List[Dict[str, str]]:
        """Check for hardcoded credentials and secrets."""
        findings = []
        for line_number, line in enumerate(source.splitlines(), 1):
            if cls.SECRET_PATTERN.search(line):
                findings.append({
                    'file': relative_path,
                    'line': str(line_number),
                    'rule': 'SEC008',
                    'severity': 'high',
                    'message': 'Possible hard-coded credential; rotate it if it is real and load it from a secret store.',
                })
            elif cls.PRIVATE_KEY_PATTERN.search(line):
                findings.append({
                    'file': relative_path,
                    'line': str(line_number),
                    'rule': 'SEC009',
                    'severity': 'critical',
                    'message': 'Private key material committed to the source tree; revoke and replace it.',
                })
            elif cls.AWS_KEY_PATTERN.search(line):
                findings.append({
                    'file': relative_path,
                    'line': str(line_number),
                    'rule': 'SEC012',
                    'severity': 'critical',
                    'message': 'AWS access key ID detected — rotate immediately and use IAM roles instead.',
                })
            elif cls.GENERIC_TOKEN_PATTERN.search(line):
                findings.append({
                    'file': relative_path,
                    'line': str(line_number),
                    'rule': 'SEC013',
                    'severity': 'high',
                    'message': 'Possible hardcoded authentication token — use environment variables or a secret manager.',
                })
        return findings

    @classmethod
    def _check_suspicious_filename(cls, filename: str, relative_path: str) -> List[Dict[str, str]]:
        """Check if the filename itself is suspicious."""
        findings = []
        name_without_ext = Path(filename).stem.lower()
        # Check full name and name without extension
        for suspicious in cls.SUSPICIOUS_FILE_NAMES:
            if suspicious in name_without_ext:
                findings.append({
                    'file': relative_path,
                    'line': '0',
                    'rule': 'FN001',
                    'severity': 'high',
                    'message': f'Suspicious filename "{filename}" contains "{suspicious}" — review for malicious intent.',
                })
                break  # One finding per file for name
        return findings

    @classmethod
    def _categorize_finding(cls, rule: str) -> str:
        """Map a rule ID to its category."""
        prefix = rule[:2].upper()
        if prefix in ('MA',):
            return 'MAL'
        if prefix in ('RS',):
            return 'RS'
        if prefix in ('CM',):
            return 'CM'
        if prefix in ('DE',):
            return 'DE'
        if prefix in ('BD',):
            return 'BD'
        if prefix in ('OB',):
            return 'OBF'
        if prefix in ('EX',):
            return 'EXP'
        if prefix in ('DC',):
            return 'DC'
        if prefix in ('FR',):
            return 'FR'
        if prefix in ('FN',):
            return 'FR'
        return 'SEC'

    @classmethod
    def _calculate_risk_score(cls, findings: List[Dict[str, str]]) -> int:
        """Calculate overall risk score from findings."""
        score = 0
        for f in findings:
            score += cls.SEVERITY_SCORE.get(f.get('severity', 'low'), 1)
        return score

    @classmethod
    def _risk_level(cls, score: int, *, emergency: bool = False) -> str:
        """Convert numeric risk score to a human-readable level."""
        if score >= 50:
            if emergency:
                return '🔴 CRITICAL — Immediate action required'
            return '🟠 HIGH — Review suggested'
        if score >= 30:
            return '🟠 HIGH — Review suggested'
        if score >= 15:
            return '🟡 MEDIUM — Review suggested'
        if score >= 1:
            return '🟢 LOW — Minor issues found'
        return '✅ CLEAN — No issues detected'

    @classmethod
    def _risk_band(cls, score: int, *, emergency: bool = False) -> str:
        """CSS/UI band for the score, after emergency/heuristic wording is applied."""
        level = cls._risk_level(score, emergency=emergency)
        if 'CRITICAL' in level or (emergency and score >= 50):
            return 'critical'
        if 'HIGH' in level:
            return 'high'
        if 'MEDIUM' in level:
            return 'medium'
        if 'LOW' in level:
            return 'low'
        return 'clean'

    @classmethod
    def apply_malware_scan_context(
        cls, result: Dict[str, Any], malware_scan: Dict[str, Any] | None
    ) -> Dict[str, Any]:
        """Soften heuristic-only CRITICAL labels when Microsoft Defender is clean.

        Defender threats still escalate to an emergency. Malware, reverse-shell,
        and backdoor keyword hits are not capped.
        """
        scan = malware_scan or {}
        status = scan.get('status')
        findings = result.get('findings') or []
        emergency = any(
            cls._categorize_finding(f.get('rule', '')) in cls.EMERGENCY_CATEGORIES
            for f in findings
        )
        score = int(result.get('risk_score') or 0)
        result['risk_band'] = cls._risk_band(score, emergency=emergency)
        if status == 'threats':
            result['risk_level_before_cap'] = result.get('risk_level')
            result['risk_level'] = '🔴 CRITICAL — Immediate action required'
            result['risk_band'] = 'critical'
            result['risk_capped'] = False
            return result
        if status == 'clean' and score >= 15 and not emergency:
            result['risk_level_before_cap'] = result.get('risk_level')
            result['risk_level'] = '🟡 MEDIUM — Review suggested'
            result['risk_band'] = 'medium'
            result['risk_capped'] = True
            result['cap_reason'] = (
                'Microsoft Defender found no detections. These alerts are from keyword '
                'matches in source and can be false positives. Review them before treating this as an emergency.'
            )
        return result

    @classmethod
    def scan(cls, root: Path) -> Dict[str, Any]:
        """Scan a project directory for security issues, malware indicators,
        exploit patterns, and safety risks.

        Returns a structured report with categorized findings and risk score.
        """
        all_findings: List[Dict[str, str]] = []
        files_scanned = 0
        truncated = False
        suspicious_files: List[Dict[str, str]] = []

        for current, directories, filenames in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            directories[:] = [
                name for name in directories
                if name.lower() not in cls.SKIP_DIRS and not name.startswith('.')
            ]
            if depth >= cls.MAX_DEPTH:
                directories.clear()

            for filename in filenames:
                path = current_path / filename
                relative_path = path.relative_to(root).as_posix()

                # Check suspicious filenames
                name_lower = filename.lower()
                for suspicious in cls.SUSPICIOUS_FILE_NAMES:
                    if suspicious in Path(filename).stem.lower():
                        suspicious_files.append({
                            'file': relative_path,
                            'reason': f'Filename contains "{suspicious}"',
                        })
                        break

                # Skip non-source files for content scanning
                if path.suffix.lower() not in cls.SOURCE_EXTENSIONS:
                    continue
                if files_scanned >= cls.MAX_FILES:
                    truncated = True
                    break

                try:
                    with path.open('rb') as source_file:
                        content = source_file.read(cls.MAX_FILE_BYTES + 1)
                    if len(content) > cls.MAX_FILE_BYTES or b'\0' in content:
                        continue
                    source = content.decode('utf-8', errors='replace')
                except OSError:
                    continue

                files_scanned += 1
                is_doc = cls._is_doc_path(relative_path)

                # Credential checks run on every text source, including docs.
                all_findings.extend(cls._check_credentials(source, relative_path))
                if is_doc:
                    continue

                # Python AST analysis
                if path.suffix.lower() == '.py':
                    all_findings.extend(cls._python_findings(source, relative_path))

                # JavaScript/TypeScript rules
                if path.suffix.lower() in {'.js', '.jsx', '.ts', '.tsx'}:
                    for line_number, line in enumerate(source.splitlines(), 1):
                        for pattern, rule, severity, message in cls.JS_RULES:
                            if pattern.search(line):
                                all_findings.append({
                                    'file': relative_path,
                                    'line': str(line_number),
                                    'rule': rule,
                                    'severity': severity,
                                    'message': message,
                                })

                # Malware pattern checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.MALWARE_PATTERNS))

                # Reverse shell checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.REVERSE_SHELL_PATTERNS))

                # Crypto miner checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.CRYPTO_MINER_PATTERNS))

                # Data exfiltration checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.DATA_EXFIL_PATTERNS))

                # Backdoor checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.BACKDOOR_PATTERNS))

                # Obfuscation checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.OBFUSCATION_PATTERNS))

                # Exploit checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.EXPLOIT_PATTERNS))

                # Dangerous command checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.DANGEROUS_CMD_PATTERNS))

                # File risk checks
                all_findings.extend(cls._check_patterns(source, relative_path, cls.FILE_RISK_PATTERNS))

            if truncated:
                break

        # Deduplicate findings (same file, line, rule)
        seen = set()
        unique_findings = []
        for f in all_findings:
            key = (f['file'], f['line'], f['rule'])
            if key not in seen:
                seen.add(key)
                f.setdefault('fixable', False)
                f.setdefault('fix_hint', '')
                unique_findings.append(f)

        # Categorize findings
        categories: Dict[str, List[Dict[str, str]]] = {}
        for f in unique_findings:
            cat = cls._categorize_finding(f['rule'])
            categories.setdefault(cat, []).append(f)

        # Calculate risk score
        emergency = any(
            cls._categorize_finding(f['rule']) in cls.EMERGENCY_CATEGORIES
            for f in unique_findings
        )
        risk_score = cls._calculate_risk_score(unique_findings)
        risk_level = cls._risk_level(risk_score, emergency=emergency)
        risk_band = cls._risk_band(risk_score, emergency=emergency)

        # Build category summary
        category_summary = {}
        for cat_code, cat_findings in sorted(categories.items()):
            label = cls.CATEGORY_LABEL.get(cat_code, cat_code)
            severities = [f['severity'] for f in cat_findings]
            worst = 'critical' if 'critical' in severities else 'high' if 'high' in severities else 'medium' if 'medium' in severities else 'low'
            category_summary[label] = {
                'count': len(cat_findings),
                'worst_severity': worst,
            }

        # Build recommendations
        recommendations = cls._generate_recommendations(categories, suspicious_files)

        return {
            'files_scanned': files_scanned,
            'truncated': truncated,
            'total_findings': len(unique_findings),
            'risk_score': risk_score,
            'risk_level': risk_level,
            'risk_band': risk_band,
            'risk_capped': False,
            'category_summary': category_summary,
            'suspicious_files': suspicious_files,
            'recommendations': recommendations,
            'findings': unique_findings,
            'fixable_count': sum(1 for f in unique_findings if f.get('fixable')),
            'scope': 'Heuristic source scan only; no code execution, exploit testing, or dependency CVE audit. '
                     'This is a static analysis tool — false positives are possible.',
        }

    @classmethod
    def apply_fixes(cls, root: Path) -> Dict[str, Any]:
        """Rewrite findings that have a safe mechanical fix, then scan again.

        The only automatic edit is ``shell=True`` on a subprocess call whose
        command is already a list of strings. Everything else is left in place.
        """
        before = cls.scan(root)
        applied: List[Dict[str, str]] = []
        files_changed = 0
        root_resolved = root.resolve()

        by_file: Dict[str, List[Dict[str, str]]] = {}
        for finding in before['findings']:
            if finding.get('fixable'):
                by_file.setdefault(finding['file'], []).append(finding)

        for relative in by_file:
            candidate = (root_resolved / relative).resolve()
            if not candidate.is_file() or not candidate.is_relative_to(root_resolved):
                continue
            try:
                raw = candidate.read_bytes()
                source = raw.decode('utf-8-sig')
            except (OSError, UnicodeDecodeError):
                continue
            updated, edits = cls._disable_shell_on_lists(source)
            if not edits or updated == source:
                continue
            bom = b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
            try:
                candidate.write_bytes(bom + updated.encode('utf-8'))
            except OSError:
                continue
            files_changed += 1
            for edit_line in edits:
                applied.append({
                    'file': relative,
                    'line': str(edit_line),
                    'rule': 'SEC007',
                    'change': 'shell=True switched to shell=False',
                })

        skipped: Dict[str, Dict[str, Any]] = {}
        for finding in before['findings']:
            if finding.get('fixable'):
                continue
            bucket = skipped.setdefault(finding['rule'], {
                'rule': finding['rule'],
                'count': 0,
                'reason': finding.get('fix_hint') or 'No automatic rewrite is safe for this rule.',
            })
            bucket['count'] += 1

        report = cls.scan(root)
        report['fix_result'] = {
            'applied': applied,
            'files_changed': files_changed,
            'skipped': list(skipped.values()),
        }
        return report

    @staticmethod
    def _disable_shell_on_lists(source: str) -> Tuple[str, List[int]]:
        """Switch shell=True to shell=False where the command is a list of strings."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return source, []
        line_numbers = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = SecurityScanner._call_name(node.func)
            if not call_name.startswith('subprocess.'):
                continue
            arg = node.args[0] if node.args else None
            if not isinstance(arg, (ast.List, ast.Tuple)):
                continue
            if not all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in arg.elts):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == 'shell'
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    line_numbers.append(keyword.lineno)
        if not line_numbers:
            return source, []
        lines = source.splitlines(keepends=True)
        edited = []
        shell_true = re.compile(r'shell\s*=\s*True')
        for line_number in line_numbers:
            if line_number < 1 or line_number > len(lines):
                continue
            line = lines[line_number - 1]
            if len(shell_true.findall(line)) != 1:
                continue
            lines[line_number - 1] = shell_true.sub('shell=False', line, count=1)
            edited.append(line_number)
        return ''.join(lines), edited

    @classmethod
    def _generate_recommendations(
        cls,
        categories: Dict[str, List[Dict[str, str]]],
        suspicious_files: List[Dict[str, str]],
    ) -> List[str]:
        """Generate actionable recommendations based on findings."""
        recs = []

        if 'RS' in categories:
            recs.append('🚨 CRITICAL: Reverse shell patterns detected — isolate this project immediately and do not execute any code from it.')

        if 'MAL' in categories:
            recs.append('🚨 CRITICAL: Malware indicators found — scan with Microsoft Defender and consider quarantining this project.')

        if 'BD' in categories:
            recs.append('🚨 CRITICAL: Backdoor patterns detected — review all network listeners and persistence mechanisms.')

        if 'CM' in categories:
            recs.append('⚠️ HIGH: Cryptocurrency mining code detected — remove mining libraries and pool connections.')

        if 'DE' in categories:
            recs.append('⚠️ HIGH: Data exfiltration patterns found — audit all network requests for sensitive data transmission.')

        if 'OBF' in categories:
            recs.append('⚠️ HIGH: Code obfuscation detected — deobfuscate and review the hidden code for malicious intent.')

        if 'EXP' in categories:
            recs.append('Review suggested: some source lines matched exploit-like keywords (for example unsafe C string functions, injection, or path traversal). Confirm they are real issues before changing the project.')

        if 'SEC' in categories:
            sec_findings = categories['SEC']
            has_creds = any(f['rule'] in ('SEC008', 'SEC009', 'SEC012', 'SEC013') for f in sec_findings)
            if has_creds:
                recs.append('⚠️ HIGH: Hardcoded credentials found — rotate all exposed secrets and use environment variables or a secret manager.')

        if 'DC' in categories:
            recs.append('⚠️ MEDIUM: Dangerous system commands detected — review and sanitize all system command usage.')

        if 'FR' in categories or suspicious_files:
            recs.append('⚠️ MEDIUM: Suspicious file operations or filenames detected — review file access patterns.')

        if not recs:
            recs.append('✅ No critical security issues detected. Regular code review is still recommended.')

        return recs
