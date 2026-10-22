import json
from pathlib import Path

#: Extra knowledge can be dropped into this directory as JSON files, each
#: holding a mapping of ``signature -> {name, cause, fix}``.
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


class KnowledgeBase:

    KNOWN_FAILURES = {

        "python 2.7": {
            "name": "Legacy Python Prerequisite",
            "cause": "Project expects an obsolete Python check",
            "fix": "Patch or bypass the Python 2 prerequisite"
        },

        "No module named": {
            "name": "Missing Python Package",
            "cause": "Required dependency is not installed",
            "fix": "Install package in active virtual environment"
        },

        "Too many levels of symbolic links": {
            "name": "Self Aware Build System",
            "cause": "Symbolic link points to itself",
            "fix": "Delete recursive symlink and rebuild target"
        },

        "symbolic links": {
            "name": "Broken Symlink",
            "cause": "Invalid symlink detected",
            "fix": "Repair or remove broken symlink"
        },

        "constexpr": {
            "name": "Old Source vs New Compiler",
            "cause": "Legacy source conflicts with modern GCC",
            "fix": "Rename deprecated identifier"
        },

        "toolchain//headers": {
            "name": "Missing LIBC Provider",
            "cause": "LIBC variable expands empty",
            "fix": "Verify LIBC_USE_GLIBC and USE_GLIBC"
        },

        "Zone.Identifier": {
            "name": "Windows ADS Metadata",
            "cause": "Windows metadata files imported into source tree",
            "fix": "Remove Zone.Identifier files"
        },

        "Expected '=' in /etc/wsl.conf": {
            "name": "Corrupted WSL Configuration",
            "cause": "Malformed wsl.conf syntax",
            "fix": "Repair configuration file"
        },

        "Command not found": {
            "name": "Missing Tool",
            "cause": "Required command unavailable",
            "fix": "Install missing package or add to PATH"
        },

        "npm ERR!": {
            "name": "Node Dependency Failure",
            "cause": "NPM package installation issue",
            "fix": "Run npm install and inspect log"
        },

        "dotnet restore": {
            "name": ".NET Dependency Failure",
            "cause": "NuGet package restore problem",
            "fix": "Run dotnet restore manually"
        },

        "Permission denied": {
            "name": "Permission Problem",
            "cause": "Insufficient filesystem permissions",
            "fix": "Verify permissions and ownership"
        },

        "CMake Error": {
            "name": "CMake Configuration Problem",
            "cause": "Build configuration failure",
            "fix": "Review generator and root project"
        }
    }

    @classmethod
    def extra_failures(cls):
        """Failures loaded from the knowledge/ directory, if it has any."""
        extras = {}
        if not KNOWLEDGE_DIR.is_dir():
            return extras

        for path in sorted(KNOWLEDGE_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for signature, entry in data.items():
                if isinstance(entry, dict) and {"name", "cause", "fix"} <= set(entry):
                    extras[signature] = dict(entry)
        return extras

    @classmethod
    def all_failures(cls):
        """Built-in knowledge first (its order is deliberate), then extras."""
        merged = dict(cls.KNOWN_FAILURES)
        merged.update(cls.extra_failures())
        return merged

    @classmethod
    def iter_matches(cls, text):
        """Yield (signature, data) for every known failure present in text.

        Longest signature first, so a specific match ("Too many levels of
        symbolic links") is reported ahead of a general one ("symbolic links").
        """
        if not text:
            return

        lowered = text.lower()
        failures = cls.all_failures()
        for signature in sorted(failures, key=len, reverse=True):
            if signature.lower() in lowered:
                yield signature, failures[signature]

    @classmethod
    def lookup(cls, text):

        if not text:
            return None

        text = text.lower()

        for signature, data in cls.all_failures().items():

            if signature.lower() in text:
                return data

        return None

    @classmethod
    def print_match(cls, text):

        result = cls.lookup(text)

        if not result:
            print("No known failure matched.")
            return

        print()
        print("=" * 40)
        print("KNOWN FAILURE DETECTED")
        print("=" * 40)

        print(f"Name : {result['name']}")
        print(f"Cause: {result['cause']}")
        print(f"Fix  : {result['fix']}")

        print("=" * 40)