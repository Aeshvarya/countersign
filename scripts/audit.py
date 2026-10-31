"""A10: the audit before the repository goes public (WORKPLAN: 11-08, 11-11, 11-12).

    python scripts/audit.py            # tracked files and the whole git history
    python scripts/audit.py --tests    # and run the offline test suite

FAIL (exit 1) on anything that must not be public:
  - credentials: Anthropic, GitHub, Slack, AWS keys, private key blocks, and any
    secret-looking variable (…KEY, …SECRET, …TOKEN) assigned a value in a tracked file
  - a .env file tracked, or .env missing from .gitignore
  - a card number other than Airwallex's published test cards (PROGRESS rule 6)
  - the same credential patterns anywhere in the git history
  - a README or PROGRESS link to a repository file that does not exist
  - Kit 10 evidence (R1-R9) pointing at a file that is gone
WARN (exit 0) on what a person should look at: e-mail addresses outside the
allowed demo domains, model ids in code, TODO/FIXME, files over 1 MB.

Nothing it finds is printed in full: a match shows as its first characters only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CREDENTIALS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
# NAME=value or NAME: value, where NAME looks secret and the value is not empty or a placeholder.
ASSIGNED_SECRET = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PRIVATE_KEY|SIGNING_KEY|PASSWORD))[ \t]*[=:][ \t]*['\"]?([^\s'\"#]{12,})",
    re.MULTILINE)
PLACEHOLDER = re.compile(r"^(?:<.*>|\$\{?[A-Z_]+\}?|x+|\.\.\.|changeme|your[-_].*)$", re.IGNORECASE)
# Airwallex's published sandbox test cards; nothing else that looks like a card may be in the repository.
TEST_CARDS = {"4035501000000008", "2223000048400011", "4000000000000002", "4242424242424242"}
CARD = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
EMAIL_OK = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|countersign\.dev|anthropic\.com|users\.noreply\.github\.com)$")
MODEL_ID = re.compile(r"\bclaude-(?:opus|sonnet|haiku|fable)-[0-9][0-9a-z.\-]*")
MARKDOWN_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff", ".woff2", ".zip"}

# Where a reader can see each Kit 10 requirement met. A missing file is a FAIL.
EVIDENCE = {
    "R1 merchant access, sandbox storefront": ["scripts/ac_onboard.py", "src/countersign/web/host.py"],
    "R2 100+ products in Airwallex's format": ["src/countersign/catalog_gmc.py", "scripts/upload_catalog.py",
                                              "tests/test_catalog_gmc.py"],
    "R3 merchant MCP in Claude": ["src/countersign/web/merchant_mcp.py", "tests/test_merchant_mcp.py"],
    "R4 confirm terms, ask again on any change": ["src/countersign/decide.py", "src/countersign/edge/acp.py",
                                                 "tests/test_decide.py", "tests/test_acp.py"],
    "R5 card only in Airwallex's component": ["src/countersign/payments.py", "src/countersign/web/static/shopper.js",
                                             "tests/test_shopper_page.py"],
    "R6 merchant order number with the outcome": ["src/countersign/merchant.py", "src/countersign/receipt.py",
                                                 "tests/test_receipt.py"],
    "R7 changed price / out of stock, no duplicate charge": ["src/countersign/web/levers.py", "tests/test_console.py",
                                                            "tests/test_checkout.py"],
    "R8 video": [],
    "R9 retry rules, stop when unverifiable": ["src/countersign/edge/idempotency.py", "src/countersign/checkout.py",
                                              "tests/test_hmac_idempotency.py"],
}


@dataclass
class Finding:
    level: str  # FAIL or WARN
    what: str
    where: str


def _git(*args: str, root: Path) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def _hint(text: str) -> str:
    return text[:6] + "…" if len(text) > 6 else text


def luhn(digits: str) -> bool:
    total, odd = 0, True
    for ch in reversed(digits):
        d = int(ch)
        if not odd:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        odd = not odd
    return total % 10 == 0


def scan_text(path: str, text: str, *, is_test: bool) -> list[Finding]:
    found: list[Finding] = []
    for name, pattern in CREDENTIALS.items():
        for m in pattern.finditer(text):
            found.append(Finding("FAIL", f"{name} ({_hint(m.group(0))})", path))
    for m in ASSIGNED_SECRET.finditer(text):
        value = m.group(2)
        if not PLACEHOLDER.match(value) and not is_test:
            found.append(Finding("FAIL", f"{m.group(1)} has a value ({_hint(value)})", path))
    for m in CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn(digits) and digits not in TEST_CARDS and len(set(digits)) > 1:
            found.append(Finding("FAIL", f"card-like number ({_hint(digits)}), not an Airwallex test card", path))
    for m in EMAIL.finditer(text):
        if not EMAIL_OK.search(m.group(1).lower()):
            found.append(Finding("WARN", f"e-mail address at {m.group(1)}", path))
    if path.endswith((".py", ".js")) and not is_test:
        for m in MODEL_ID.finditer(text):
            found.append(Finding("WARN", f"model id in code ({m.group(0)}): keep it in configuration", path))
    if re.search(r"\b(?:TODO|FIXME|XXX)\b", text) and not path.endswith("audit.py"):
        found.append(Finding("WARN", "TODO/FIXME left in", path))
    return found


def scan_tree(root: Path) -> list[Finding]:
    found: list[Finding] = []
    files = [f for f in _git("ls-files", "-z", root=root).split("\0") if f]
    for name in files:
        path = root / name
        if Path(name).name == ".env" or name.endswith("/.env"):
            found.append(Finding("FAIL", ".env is tracked", name))
        if not path.is_file() or path.suffix.lower() in BINARY:
            continue
        if path.stat().st_size > 1_000_000:
            found.append(Finding("WARN", f"large file ({path.stat().st_size // 1024} KB)", name))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found += scan_text(name, text, is_test=name.startswith("tests/") or name == "scripts/audit.py")
    ignore = (root / ".gitignore").read_text() if (root / ".gitignore").exists() else ""
    if not re.search(r"^\.env$", ignore, re.MULTILINE):
        found.append(Finding("FAIL", ".env is not in .gitignore", ".gitignore"))
    return found


def scan_history(root: Path) -> list[Finding]:
    """Credential patterns in every version of every file, on every branch."""
    found: list[Finding] = []
    log = _git("log", "--all", "-p", "--no-color", "--format=commit %H", root=root)
    commit = "?"
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line[7:19]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for name, pattern in CREDENTIALS.items():
            m = pattern.search(line)
            if m:
                found.append(Finding("FAIL", f"{name} in history ({_hint(m.group(0))})", f"commit {commit}"))
    return found


def scan_links(root: Path) -> list[Finding]:
    found: list[Finding] = []
    for doc in ("README.md", "PROGRESS.md", "WORKPLAN.md", "SPEC.md"):
        path = root / doc
        if not path.exists():
            continue
        for m in MARKDOWN_LINK.finditer(path.read_text(encoding="utf-8")):
            if not (root / m.group(1)).exists():
                found.append(Finding("FAIL", f"link to a missing file: {m.group(1)}", doc))
    return found


def scan_evidence(root: Path) -> tuple[list[Finding], list[str]]:
    found, lines = [], []
    for requirement, paths in EVIDENCE.items():
        missing = [p for p in paths if not (root / p).exists()]
        for p in missing:
            found.append(Finding("FAIL", f"{requirement}: evidence file missing", p))
        lines.append(f"  {requirement}: " + (", ".join(paths) if paths else "the video (A9)"))
    return found, lines


def audit(root: Path = ROOT, *, history: bool = True) -> tuple[list[Finding], list[str]]:
    findings = scan_tree(root) + scan_links(root)
    if history:
        findings += scan_history(root)
    evidence_findings, evidence = scan_evidence(root)
    return findings + evidence_findings, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the repository before it goes public.")
    parser.add_argument("--tests", action="store_true", help="also run the offline test suite")
    parser.add_argument("--no-history", action="store_true", help="skip the git history scan")
    args = parser.parse_args()
    findings, evidence = audit(history=not args.no_history)
    for f in sorted(findings, key=lambda f: (f.level != "FAIL", f.where)):
        print(f"{f.level}  {f.where}: {f.what}")
    print("\nKit 10 evidence:")
    print("\n".join(evidence))
    failed = sum(f.level == "FAIL" for f in findings)
    ok = failed == 0
    if args.tests:
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT,
                             capture_output=True, text=True)
        tail = (run.stderr or run.stdout).strip().splitlines()[-1:]
        print(f"\ntests: {'PASS' if run.returncode == 0 else 'FAIL'} {' '.join(tail)}")
        ok = ok and run.returncode == 0
    print(f"\n{failed} to fix, {sum(f.level == 'WARN' for f in findings)} to look at")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
