"""Runtime security guard."""

import re
from typing import List, NamedTuple


class SecurityViolation(NamedTuple):
    rule_id: str
    description: str
    match_content: str


class SecretScanner:
    """Scans content for potential secrets."""

    # Common secret patterns
    PATTERNS = {
        "aws_key": re.compile(r"(?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9])"),
        "aws_secret": re.compile(
            r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
        ),
        "private_key": re.compile(r"-----BEGIN [A-Z]+ PRIVATE KEY-----"),
        "generic_api_key": re.compile(
            r"(?i)(api_key|apikey|secret|token)\s*[:=]\s*['\"]([A-Za-z0-9_\-]{16,})['\"]"
        ),
    }

    def scan(self, content: str) -> List[SecurityViolation]:
        violations = []
        for name, pattern in self.PATTERNS.items():
            for match in pattern.finditer(content):
                # For generic keys, we want the value group if it exists
                if name == "generic_api_key":
                    matched_text = match.group(2)  # The value inside quotes
                else:
                    matched_text = match.group(0)

                # Basic entropy check or further validation could go here
                # For now, flag it.

                # Redact in report
                redacted = (
                    matched_text[:2] + "*" * (len(matched_text) - 4) + matched_text[-2:]
                )

                violations.append(
                    SecurityViolation(
                        rule_id=f"secret_{name}",
                        description=f"Potential {name} detected",
                        match_content=redacted,
                    )
                )
        return violations


def scan_patch(patch_content: str) -> List[SecurityViolation]:
    """Helper to scan a patch file content."""
    scanner = SecretScanner()
    return scanner.scan(patch_content)
