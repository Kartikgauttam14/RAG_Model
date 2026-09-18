import re
from dataclasses import dataclass

INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"reveal\s+(the\s+)?system\s+prompt",
        r"developer\s+message",
        r"act\s+as\s+(an?\s+)?unrestricted",
        r"override\s+(the\s+)?(system|safety|policy)",
        r"execute\s+(this|the following)\s+(tool|command)",
        r"<\s*(system|assistant|tool)\s*>",
    )
]


@dataclass(frozen=True)
class InjectionAssessment:
    suspicious: bool
    matches: tuple[str, ...]


def assess_prompt_injection(text: str) -> InjectionAssessment:
    matches = tuple(pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(text))
    return InjectionAssessment(bool(matches), matches)


def wrap_untrusted_evidence(text: str, chunk_id: str) -> str:
    return f'<evidence chunk_id="{chunk_id}" trust="untrusted-data">\n{text}\n</evidence>'
