"""Scanner tuning: language-aware C rules, docs skip, Defender score cap."""
import tempfile
from pathlib import Path

from core.security import SecurityScanner


def _proj(name, files):
    d = Path(tempfile.mkdtemp()) / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        path = d / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return d


def test_go_sprintf_is_not_flagged_as_c_buffer_overflow():
    d = _proj("go-fmt", {"main.go": 'package main\nimport "fmt"\nfunc main() { fmt.Sprintf("%t", true) }\n'})
    result = SecurityScanner.scan(d)
    rules = {f["rule"] for f in result["findings"]}
    assert "EXP001" not in rules
    assert result["risk_score"] == 0
    assert "CLEAN" in result["risk_level"]


def test_c_strcpy_is_still_flagged():
    d = _proj("c-buf", {"overflow.c": 'void f(char *src) { char buf[8]; strcpy(buf, src); }\n'})
    result = SecurityScanner.scan(d)
    hits = [f for f in result["findings"] if f["rule"] == "EXP001"]
    assert hits
    assert hits[0]["file"] == "overflow.c"
    assert hits[0]["severity"] == "high"


def test_docs_and_readme_keyword_hits_are_not_critical():
    d = _proj("docs-noise", {
        "README.md": "This guide explains strcpy() and sprintf() buffer overflows.\nAlso eval() and rm -rf /.\n",
        "docs/usage.go": 'package docs\nimport "fmt"\nvar s = fmt.Sprintf("https://example/%s", id)\n',
        "docs/exploit-notes.py": "os.system('rm -rf /')\neval(payload)\n",
        "readme.sh": "sprintf(buf, input)\n",
    })
    result = SecurityScanner.scan(d)
    exp = [f for f in result["findings"] if f["rule"].startswith("EXP") or f["rule"].startswith("DC")]
    assert exp == []
    assert result["risk_score"] < 50
    assert "Immediate action required" not in result["risk_level"]


def test_heuristic_high_score_says_review_suggested():
    files = {f"hit{i}.c": "strcpy(buf, src);\n" for i in range(9)}
    d = _proj("many-strcpy", files)
    result = SecurityScanner.scan(d)
    assert result["risk_score"] == 63  # 9 high findings * 7
    assert result["risk_level"] == "🟠 HIGH — Review suggested"
    assert result["risk_band"] == "high"
    assert "Immediate action required" not in result["risk_level"]


def test_defender_clean_caps_heuristic_critical_wording():
    files = {f"hit{i}.c": "strcpy(buf, src);\n" for i in range(9)}
    d = _proj("capped", files)
    result = SecurityScanner.scan(d)
    adjusted = SecurityScanner.apply_malware_scan_context(
        result,
        {"status": "clean", "message": "Microsoft Defender completed the custom scan with no matching detections."},
    )
    assert adjusted["risk_capped"] is True
    assert adjusted["risk_level"] == "🟡 MEDIUM — Review suggested"
    assert adjusted["risk_band"] == "medium"
    assert "cap_reason" in adjusted
    assert "Immediate action required" not in adjusted["risk_level"]


def test_defender_threats_keep_immediate_action():
    d = _proj("threats", {"ok.py": "print('hi')\n"})
    result = SecurityScanner.scan(d)
    adjusted = SecurityScanner.apply_malware_scan_context(
        result,
        {"status": "threats", "message": "Threats were detected in the scanned path."},
    )
    assert adjusted["risk_level"] == "🔴 CRITICAL — Immediate action required"
    assert adjusted["risk_band"] == "critical"


def test_malware_keyword_hits_are_not_capped_when_defender_is_clean():
    # A real malware-category rule should still be treated as an emergency.
    d = _proj("mal", {"dropper.py": "import ctypes\n"})
    # If the tree has no MAL finding, skip the emergency assertion and just
    # document that only EMERGENCY_CATEGORIES bypass the cap.
    result = {
        "risk_score": 63,
        "risk_level": "🟠 HIGH — Review suggested",
        "risk_band": "high",
        "risk_capped": False,
        "findings": [{"file": "dropper.py", "line": "1", "rule": "MAL001", "severity": "high", "message": "test"}],
    }
    adjusted = SecurityScanner.apply_malware_scan_context(result, {"status": "clean"})
    assert adjusted["risk_capped"] is not True
    assert adjusted["risk_level"] == "🟠 HIGH — Review suggested"
