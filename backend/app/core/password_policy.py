"""
Enterprise password policy for ResearchSphere AI.

Reusable validators so signup, password reset and any future
change-password flow enforce exactly the same rules.

The policy is configurable; the defaults follow current NIST guidance:
length carries most of the strength, and a password is also checked against
common-password and context (email/name) lists rather than relying purely on
character-class rules.
"""

import math
import re
from dataclasses import dataclass, field

from app.core.config import settings

# ---------------------------------------------------------------------------
# Common password list
# ---------------------------------------------------------------------------
#: Compact list of the passwords that dominate credential-stuffing corpora.
#: Kept in-process deliberately: a network call in the signup path would be a
#: latency and availability dependency on a security check.
COMMON_PASSWORDS = {
    "123456",
    "123456789",
    "12345678",
    "12345",
    "1234567",
    "1234567890",
    "password",
    "password1",
    "password123",
    "passw0rd",
    "p@ssw0rd",
    "p@ssword",
    "qwerty",
    "qwerty123",
    "qwertyuiop",
    "abc123",
    "111111",
    "123123",
    "iloveyou",
    "admin",
    "administrator",
    "welcome",
    "welcome1",
    "monkey",
    "letmein",
    "login",
    "dragon",
    "sunshine",
    "princess",
    "football",
    "baseball",
    "master",
    "superman",
    "trustno1",
    "shadow",
    "michael",
    "ashley",
    "qazwsx",
    "zaq12wsx",
    "starwars",
    "whatever",
    "freedom",
    "changeme",
    "secret",
    "test123",
    "testing",
    "default",
    "guest",
    "root",
    "toor",
    "pass",
    "pass123",
    "hello123",
    "summer2024",
    "winter2024",
    "spring2024",
    "autumn2024",
    "companyname",
    "letmein123",
    "access",
    "azerty",
    "1q2w3e4r",
    "1qaz2wsx",
    "qwe123",
    "asdfgh",
    "zxcvbn",
}

#: Sequences used for the "predictable pattern" check.
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "01234567890",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
)

_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"[0-9]")
_SPECIAL = re.compile(r"[^A-Za-z0-9]")
_REPEAT = re.compile(r"(.)\1{2,}")  # three or more identical characters


class PasswordPolicyError(Exception):
    """Raised when a password fails policy. ``violations`` lists every reason."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass
class PasswordStrength:
    """Result of scoring a password."""

    score: int  # 0-100
    label: str  # very_weak | weak | fair | strong | very_strong
    entropy_bits: float
    violations: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _has_sequence(password: str, min_run: int = 4) -> bool:
    lowered = password.lower()
    for seq in _SEQUENCES:
        for i in range(len(seq) - min_run + 1):
            run = seq[i : i + min_run]
            if run in lowered or run[::-1] in lowered:
                return True
    return False


def _charset_size(password: str) -> int:
    size = 0
    if _LOWER.search(password):
        size += 26
    if _UPPER.search(password):
        size += 26
    if _DIGIT.search(password):
        size += 10
    if _SPECIAL.search(password):
        size += 32
    return size or 1


def estimate_entropy_bits(password: str) -> float:
    """Rough entropy estimate: len * log2(charset). Indicative, not exact."""
    if not password:
        return 0.0
    return round(len(password) * math.log2(_charset_size(password)), 1)


def _context_terms(email: str | None, full_name: str | None) -> list[str]:
    terms = []
    if email:
        local = email.split("@")[0]
        terms.append(local)
        domain = email.split("@")[-1].split(".")[0] if "@" in email else ""
        if domain:
            terms.append(domain)
    if full_name:
        terms.extend(full_name.split())
    return [_normalize(t) for t in terms if len(_normalize(t)) >= 4]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def evaluate_password(
    password: str,
    email: str | None = None,
    full_name: str | None = None,
) -> PasswordStrength:
    """Evaluate a password against policy and score its strength.

    Never raises - returns a ``PasswordStrength`` whose ``violations`` list is
    empty when the password is acceptable.
    """
    violations: list[str] = []
    password = password or ""

    if len(password) < settings.PASSWORD_MIN_LENGTH:
        violations.append(
            f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long."
        )
    if len(password) > settings.PASSWORD_MAX_LENGTH:
        violations.append(
            f"Password must be at most {settings.PASSWORD_MAX_LENGTH} characters long."
        )
    if settings.PASSWORD_REQUIRE_UPPERCASE and not _UPPER.search(password):
        violations.append("Password must contain at least one uppercase letter.")
    if settings.PASSWORD_REQUIRE_LOWERCASE and not _LOWER.search(password):
        violations.append("Password must contain at least one lowercase letter.")
    if settings.PASSWORD_REQUIRE_DIGIT and not _DIGIT.search(password):
        violations.append("Password must contain at least one number.")
    if settings.PASSWORD_REQUIRE_SPECIAL and not _SPECIAL.search(password):
        violations.append("Password must contain at least one special character.")

    normalized = _normalize(password)
    if settings.PASSWORD_BLOCK_COMMON:
        if password.lower() in COMMON_PASSWORDS or normalized in COMMON_PASSWORDS:
            violations.append("This password is too common. Choose something less predictable.")
        else:
            # Catch "Password123!" style variants of a common base word.
            stripped = re.sub(r"[^a-z]", "", password.lower())
            if len(stripped) >= 4 and stripped in COMMON_PASSWORDS:
                violations.append(
                    "This password is based on a very common word. Choose something less predictable."
                )

    if _REPEAT.search(password):
        violations.append("Password must not contain a character repeated three or more times.")
    if _has_sequence(password):
        violations.append(
            "Password must not contain predictable sequences such as 'abcd' or '1234'."
        )

    for term in _context_terms(email, full_name):
        if term and term in normalized:
            violations.append("Password must not contain your name or email address.")
            break

    # --- scoring -----------------------------------------------------------
    entropy = estimate_entropy_bits(password)
    score = int(min(100, (entropy / 100) * 100))
    classes = sum(bool(rx.search(password)) for rx in (_UPPER, _LOWER, _DIGIT, _SPECIAL))
    score = int(min(100, score * 0.7 + (classes / 4) * 30))
    if violations:
        score = min(score, 40)

    if score < 20:
        label = "very_weak"
    elif score < 40:
        label = "weak"
    elif score < 60:
        label = "fair"
    elif score < 80:
        label = "strong"
    else:
        label = "very_strong"

    return PasswordStrength(score=score, label=label, entropy_bits=entropy, violations=violations)


def validate_password(
    password: str,
    email: str | None = None,
    full_name: str | None = None,
) -> PasswordStrength:
    """Validate a password, raising ``PasswordPolicyError`` when it fails."""
    result = evaluate_password(password, email=email, full_name=full_name)
    if result.violations:
        raise PasswordPolicyError(result.violations)
    return result
