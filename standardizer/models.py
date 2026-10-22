from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentType(StrEnum):
    TABLE_SCHEMA = "table_schema"
    RELATIONSHIP = "relationship"
    BUSINESS_GLOSSARY = "business_glossary"
    BUSINESS_RULE = "business_rule"
    VERIFIED_QUERY = "verified_query"
    QUESTION_SQL_PAIR = "question_sql_pair"
    EDGE_CASE = "edge_case"
    FORBIDDEN_PATTERN = "forbidden_pattern"
    TEST_CASE = "test_case"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    evidence: str | None = None


class Relationship(BaseModel):
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    cardinality: Literal["1:1", "1:N", "N:1", "N:M", "unknown"] = "unknown"
    join_type: Literal["LEFT", "INNER", "RIGHT", "FULL", "unknown"] = "unknown"
    evidence: str = ""

    @field_validator("source_table", "source_column", "target_table", "target_column")
    @classmethod
    def strip_identifier(cls, value: str) -> str:
        return value.strip().strip("[]`\"")


class ColumnFact(BaseModel):
    table: str
    name: str
    data_type: str | None = None
    nullable: bool | None = None


class ObservedFacts(BaseModel):
    tables: list[str] = Field(default_factory=list)
    columns: list[ColumnFact] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    questions: list[str] = Field(default_factory=list)
    identifier_evidence: list[str] = Field(default_factory=list)
    has_select_star: bool = False
    prohibited_operations: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


class RuleProposal(BaseModel):
    rule_id: str = ""
    title: str
    description: str
    evidence: str = ""


class StandardizationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DocumentType
    title_fa: str
    domain: str
    summary_fa: str
    canonical_entity: str = ""
    grain: str = ""
    synonyms_fa: list[str] = Field(default_factory=list)
    business_rules: list[RuleProposal] = Field(default_factory=list)
    questions_fa: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    ambiguity_notes_fa: list[str] = Field(default_factory=list)

    @field_validator("title_fa", "domain", "summary_fa")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class SourceDocument(BaseModel):
    filename: str
    content: str
    source_path: str = ""
    content_hash: str


class StandardizationResult(BaseModel):
    id: str
    source: SourceDocument
    facts: ObservedFacts
    proposal: StandardizationProposal
    issues: list[Issue]
    quality_score: int = Field(ge=0, le=100)
    review_status: ReviewStatus = ReviewStatus.PENDING
    output_relative_path: str
    rendered_content: str
    llm_provider: str
    llm_model: str

    @property
    def is_blocked(self) -> bool:
        return any(issue.severity in {Severity.ERROR, Severity.BLOCKER} for issue in self.issues)


class BatchReport(BaseModel):
    batch_id: str
    input_directory: str
    output_directory: str
    total: int
    standardized: int
    skipped: int
    blocked: int
    pending_review: int
    approved: int
    average_quality_score: float
    results: list[StandardizationResult]


class StandardizeRequest(BaseModel):
    filename: str
    content: str = Field(min_length=1)
    use_llm: bool | None = None


class ReviewDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=2, max_length=100)
    note: str = Field(default="", max_length=2000)
