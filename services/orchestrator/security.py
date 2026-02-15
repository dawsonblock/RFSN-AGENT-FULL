import re
from fastapi import HTTPException

# Common jailbreak/injection patterns
JAILBREAK_PATTERNS = [
    r"ignore (all )?previous instructions",
    r"ignore the above instructions",
    r"system override",
    r"you are now (unrestricted|free|chaos)",
    r"disable (safety|security) protocols",
    r"commands:\s*(sudo|rm -rf|mkfs|dd)",  # destructive bash attempts in prompt
    r"switch to (developer|god) mode",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in JAILBREAK_PATTERNS]


def validate_input_safety(text: str) -> str:
    """
    Scans input text for indirect prompt injection or jailbreak patterns.
    Raises HTTPException(400) if a threat is detected.
    Returns the text if safe.
    """
    if not text:
        return text

    for i, pattern in enumerate(COMPILED_PATTERNS):
        if pattern.search(text):
            raise HTTPException(
                status_code=400,
                detail=f"Security Alert: Input blocked by firewall. Pattern match: {JAILBREAK_PATTERNS[i]}",
            )

    return text


# Secret Scanning Regexes (Simplified for demo)
SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub Personal Access Token"),
    (r"xox[baprs]-([0-9a-zA-Z]{10,48})?", "Slack Token"),
    (r"-----BEGIN PRIVATE KEY-----", "Private Key"),
    (r"sk-[a-zA-Z0-9]{48}", "OpenAI API Key"),
]

COMPILED_SECRETS = [(re.compile(p), name) for p, name in SECRET_PATTERNS]


def scan_for_secrets(content: str) -> None:
    """
    Scans content for hardcoded secrets.
    Raises HTTPException(400) if found.
    """
    if not content:
        return

    for pattern, name in COMPILED_SECRETS:
        if pattern.search(content):
            raise HTTPException(
                status_code=400,
                detail=f"Security Alert: Hardcoded secret detected ({name}). Commit rejected.",
            )
