from dataclasses import dataclass, field


@dataclass(frozen=True)
class Citation:
    document_id: str
    document_name: str
    document_version: int
    chunk_id: str
    page: int | None
    section: str | None
    excerpt: str


@dataclass(frozen=True)
class VerificationResult:
    supported: bool
    answered_question: bool
    unsupported_claims: list[str] = field(default_factory=list)
    citation_errors: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    recommended_action: str = "refuse"
    confidence: float = 0.0


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    confidence: float
    citations: list[Citation]
    grounded: bool
    verification_status: str
    conflicts: list[str] = field(default_factory=list)
    provider_error: str | None = None
