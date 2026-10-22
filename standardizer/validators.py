from __future__ import annotations

import re
from collections import defaultdict

from standardizer.models import (
    DocumentType,
    Issue,
    ObservedFacts,
    Severity,
    SourceDocument,
    StandardizationProposal,
    StandardizationResult,
)


def _normal(value: str) -> str:
    return value.strip().strip("[]`\"").casefold()


def _normal_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _identifier_is_grounded(identifier: str, facts: ObservedFacts, source: SourceDocument) -> bool:
    if not identifier:
        return False
    target = _normal(identifier)
    evidence = {_normal(value) for value in facts.identifier_evidence}
    evidence.update(_normal(value) for value in facts.tables)
    evidence.update(_normal(column.name) for column in facts.columns)
    if target in evidence:
        return True
    leaf = target.split(".")[-1]
    if leaf in evidence:
        return True
    pattern = rf"(?i)(?<![A-Za-z0-9_])(?:\[|`)?{re.escape(identifier.split('.')[-1])}(?:\]|`)?(?![A-Za-z0-9_])"
    return bool(re.search(pattern, source.content))


def validate_proposal(
    source: SourceDocument,
    facts: ObservedFacts,
    proposal: StandardizationProposal,
    *,
    llm_failed: str | None = None,
) -> tuple[list[Issue], int]:
    issues: list[Issue] = []
    if llm_failed:
        issues.append(
            Issue(
                code="LLM_FALLBACK",
                severity=Severity.WARNING,
                message="مدل محلی در دسترس نبود؛ نتیجه با قواعد قطعی ساخته شد.",
                evidence=llm_failed[:500],
            )
        )
    if proposal.doc_type == DocumentType.UNKNOWN:
        issues.append(
            Issue(
                code="UNKNOWN_DOCUMENT_TYPE",
                severity=Severity.ERROR,
                message="نوع سند با اطمینان مشخص نشد.",
            )
        )
    if proposal.canonical_entity and facts.tables and not _identifier_is_grounded(
        proposal.canonical_entity, facts, source
    ):
        issues.append(
            Issue(
                code="UNGROUNDED_CANONICAL_ENTITY",
                severity=Severity.ERROR,
                message=(
                    "موجودیت اصلی پیشنهادی در سند اثبات نشده است: "
                    f"{proposal.canonical_entity}"
                ),
            )
        )
    if not proposal.grain and proposal.doc_type in {
        DocumentType.TABLE_SCHEMA,
        DocumentType.VERIFIED_QUERY,
        DocumentType.QUESTION_SQL_PAIR,
    }:
        issues.append(
            Issue(
                code="MISSING_GRAIN",
                severity=Severity.WARNING,
                message="سطح رکورد یا grain مشخص نشده است.",
            )
        )
    if not facts.tables and proposal.doc_type in {
        DocumentType.TABLE_SCHEMA,
        DocumentType.RELATIONSHIP,
        DocumentType.VERIFIED_QUERY,
    }:
        issues.append(
            Issue(
                code="NO_TABLE_EVIDENCE",
                severity=Severity.ERROR,
                message="هیچ نام جدول قابل اثباتی در سند پیدا نشد.",
            )
        )
    observed_relation_keys = {
        (
            _normal(relation.source_table),
            _normal(relation.source_column),
            _normal(relation.target_table),
            _normal(relation.target_column),
        )
        for relation in facts.relationships
    }
    observed_relation_keys.update(
        (target_table, target_column, source_table, source_column)
        for source_table, source_column, target_table, target_column in list(
            observed_relation_keys
        )
    )
    for relation in proposal.relationships:
        for kind, identifier in (
            ("source_table", relation.source_table),
            ("source_column", relation.source_column),
            ("target_table", relation.target_table),
            ("target_column", relation.target_column),
        ):
            if not _identifier_is_grounded(identifier, facts, source):
                issues.append(
                    Issue(
                        code="UNGROUNDED_IDENTIFIER",
                        severity=Severity.BLOCKER,
                        message=f"شناسه پیشنهادی {kind} در منبع اثبات نشده است: {identifier}",
                        evidence=relation.evidence[:300] or None,
                    )
                )
        relation_key = (
            _normal(relation.source_table),
            _normal(relation.source_column),
            _normal(relation.target_table),
            _normal(relation.target_column),
        )
        if relation_key not in observed_relation_keys:
            issues.append(
                Issue(
                    code="UNGROUNDED_RELATIONSHIP",
                    severity=Severity.BLOCKER,
                    message=(
                        "رابطه پیشنهادی در FK، Join یا مسیر صریح سند مبدأ اثبات نشده است: "
                        f"{relation.source_table}.{relation.source_column} -> "
                        f"{relation.target_table}.{relation.target_column}"
                    ),
                    evidence=relation.evidence[:300] or None,
                )
            )
    normalized_source = _normal_text(source.content)
    for rule in proposal.business_rules:
        if not rule.evidence or _normal_text(rule.evidence) not in normalized_source:
            issues.append(
                Issue(
                    code="UNGROUNDED_BUSINESS_RULE",
                    severity=Severity.ERROR,
                    message=f"قاعده پیشنهادی شاهد لفظی معتبر ندارد: {rule.title}",
                    evidence=rule.evidence[:300] or None,
                )
            )
    if facts.has_select_star:
        severity = (
            Severity.INFO
            if proposal.doc_type == DocumentType.FORBIDDEN_PATTERN
            else Severity.ERROR
        )
        issues.append(
            Issue(
                code="SELECT_STAR",
                severity=severity,
                message="استفاده از SELECT * برای Query تأییدشده مجاز نیست.",
            )
        )
    for operation in facts.prohibited_operations:
        severity = (
            Severity.INFO
            if proposal.doc_type == DocumentType.FORBIDDEN_PATTERN
            or (
                proposal.doc_type == DocumentType.TABLE_SCHEMA
                and operation in {"CREATE TABLE", "ALTER"}
            )
            else Severity.BLOCKER
        )
        issues.append(
            Issue(
                code="PROHIBITED_SQL_OPERATION",
                severity=severity,
                message=f"عملیات SQL نیازمند بازبینی امنیتی است: {operation}",
            )
        )
    for parse_error in facts.parse_errors:
        issues.append(
            Issue(
                code="SQL_PARSE_ERROR",
                severity=Severity.ERROR,
                message="SQL با dialect ‏T-SQL به‌طور کامل parse نشد.",
                evidence=parse_error,
            )
        )
    status = str(facts.frontmatter.get("status", "")).casefold()
    if status in {"obsolete", "invalid", "rejected"}:
        issues.append(
            Issue(
                code="SOURCE_NOT_GENERATION_READY",
                severity=Severity.BLOCKER,
                message=f"وضعیت سند مبدأ برای تولید مجاز نیست: {status}",
            )
        )
    if proposal.doc_type == DocumentType.VERIFIED_QUERY and not proposal.questions_fa:
        issues.append(
            Issue(
                code="MISSING_EXAMPLE_QUESTIONS",
                severity=Severity.WARNING,
                message="برای Query تأییدشده هیچ سؤال یا بازنویسی فارسی ثبت نشده است.",
            )
        )
    if proposal.relationships and any(
        relation.cardinality == "unknown" for relation in proposal.relationships
    ):
        issues.append(
            Issue(
                code="UNKNOWN_CARDINALITY",
                severity=Severity.WARNING,
                message="Cardinality حداقل یک رابطه مشخص نیست.",
            )
        )
    if proposal.ambiguity_notes_fa:
        issues.append(
            Issue(
                code="SEMANTIC_AMBIGUITY",
                severity=Severity.WARNING,
                message="سند ابهام معنایی دارد و باید توسط کارشناس بررسی شود.",
                evidence=" | ".join(proposal.ambiguity_notes_fa[:4]),
            )
        )

    score = 100
    deductions = {
        Severity.INFO: 0,
        Severity.WARNING: 6,
        Severity.ERROR: 18,
        Severity.BLOCKER: 35,
    }
    score -= sum(deductions[issue.severity] for issue in issues)
    if proposal.grain:
        score += 2
    if proposal.questions_fa:
        score += 2
    if facts.frontmatter.get("status") == "approved":
        score += 2
    return issues, max(0, min(100, score))


def detect_batch_conflicts(results: list[StandardizationResult]) -> dict[str, list[Issue]]:
    conflicts: dict[str, list[Issue]] = defaultdict(list)
    relation_index: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
    entity_types: dict[str, list[tuple[str, DocumentType]]] = defaultdict(list)
    for result in results:
        entity = _normal(result.proposal.canonical_entity)
        if entity:
            entity_types[entity].append((result.id, result.proposal.doc_type))
        for relation in result.proposal.relationships:
            key = (_normal(relation.source_table), _normal(relation.source_column))
            relation_index[key].append(
                (
                    result.id,
                    _normal(relation.target_table),
                    _normal(relation.target_column),
                    relation.join_type,
                )
            )
    for key, entries in relation_index.items():
        targets = {(target_table, target_column) for _, target_table, target_column, _ in entries}
        join_types = {join_type for _, _, _, join_type in entries if join_type != "unknown"}
        if len(targets) > 1:
            for result_id, *_ in entries:
                conflicts[result_id].append(
                    Issue(
                        code="RELATION_TARGET_CONFLICT",
                        severity=Severity.BLOCKER,
                        message=(
                            f"برای رابطه {key[0]}.{key[1]} بیش از یک مقصد متفاوت در batch وجود دارد."
                        ),
                    )
                )
        if len(join_types) > 1:
            for result_id, *_ in entries:
                conflicts[result_id].append(
                    Issue(
                        code="JOIN_TYPE_CONFLICT",
                        severity=Severity.ERROR,
                        message=f"نوع Join رابطه {key[0]}.{key[1]} بین اسناد متناقض است.",
                    )
                )
    for entity, entries in entity_types.items():
        schema_docs = [result_id for result_id, doc_type in entries if doc_type == DocumentType.TABLE_SCHEMA]
        if len(schema_docs) > 1:
            for result_id in schema_docs:
                conflicts[result_id].append(
                    Issue(
                        code="DUPLICATE_TABLE_SCHEMA",
                        severity=Severity.ERROR,
                        message=f"بیش از یک سند ساختار جدول برای {entity} وجود دارد.",
                    )
                )
    return dict(conflicts)
