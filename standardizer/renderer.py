from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from standardizer.models import DocumentType, StandardizationResult

_FOLDERS: dict[DocumentType, str] = {
    DocumentType.TABLE_SCHEMA: "01_schema/tables",
    DocumentType.RELATIONSHIP: "02_relationships",
    DocumentType.BUSINESS_GLOSSARY: "03_business_glossary",
    DocumentType.BUSINESS_RULE: "04_business_rules",
    DocumentType.VERIFIED_QUERY: "05_verified_queries",
    DocumentType.QUESTION_SQL_PAIR: "06_question_sql_pairs",
    DocumentType.EDGE_CASE: "07_edge_cases",
    DocumentType.FORBIDDEN_PATTERN: "08_forbidden_patterns",
    DocumentType.TEST_CASE: "09_test_cases",
    DocumentType.UNKNOWN: "99_review_required",
}


def slugify(value: str) -> str:
    value = value.strip().replace(".", "_")
    value = re.sub(r"[^\w\u0600-\u06FF-]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:120] or "document"


def output_path_for(result: StandardizationResult) -> str:
    folder = _FOLDERS[result.proposal.doc_type]
    base = slugify(result.proposal.canonical_entity or Path(result.source.filename).stem)
    extension = ".sql" if result.proposal.doc_type == DocumentType.VERIFIED_QUERY else ".md"
    return f"{folder}/{base}_{result.source.content_hash[:8]}{extension}"


def _metadata(result: StandardizationResult, *, status: str = "draft") -> dict[str, object]:
    return {
        "doc_type": result.proposal.doc_type.value,
        "document_id": result.id,
        "version": "1.0",
        "status": status,
        "language": "fa-IR",
        "dialect": "tsql",
        "domain": result.proposal.domain,
        "canonical_entity": result.proposal.canonical_entity or None,
        "source_file": result.source.filename,
        "source_hash": result.source.content_hash,
        "quality_score": result.quality_score,
        "requires_human_review": status != "approved",
    }


def render_result(result: StandardizationResult, *, status: str = "draft") -> str:
    if result.proposal.doc_type == DocumentType.VERIFIED_QUERY:
        return _render_verified_query(result, status=status)
    return _render_markdown(result, status=status)


def _render_markdown(result: StandardizationResult, *, status: str) -> str:
    metadata = yaml.safe_dump(
        _metadata(result, status=status),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    lines = [
        "---",
        metadata,
        "---",
        "",
        f"# {result.proposal.title_fa}",
        "",
        "## خلاصه",
        "",
        result.proposal.summary_fa,
        "",
    ]
    if result.proposal.grain:
        lines.extend(["## سطح رکورد (Grain)", "", result.proposal.grain, ""])
    if result.facts.tables:
        lines.extend(["## جدول‌های اثبات‌شده", ""])
        lines.extend(f"- `{table}`" for table in result.facts.tables)
        lines.append("")
    if result.facts.columns:
        lines.extend(
            [
                "## ستون‌های استخراج‌شده",
                "",
                "| جدول | ستون | نوع | Nullable |",
                "|---|---|---|---|",
            ]
        )
        for column in result.facts.columns:
            nullable = "نامشخص" if column.nullable is None else ("بله" if column.nullable else "خیر")
            lines.append(
                f"| `{column.table}` | `{column.name}` | `{column.data_type or 'نامشخص'}` | {nullable} |"
            )
        lines.append("")
    if result.proposal.relationships:
        lines.extend(
            [
                "## روابط",
                "",
                "| مبدأ | مقصد | Cardinality | Join | شاهد |",
                "|---|---|---|---|---|",
            ]
        )
        for relation in result.proposal.relationships:
            evidence = relation.evidence.replace("|", "\\|").replace("\n", " ")[:180]
            lines.append(
                f"| `{relation.source_table}.{relation.source_column}` | "
                f"`{relation.target_table}.{relation.target_column}` | "
                f"{relation.cardinality} | {relation.join_type} | {evidence} |"
            )
        lines.append("")
    if result.proposal.synonyms_fa:
        lines.extend(["## مترادف‌های فارسی", ""])
        lines.extend(f"- {item}" for item in result.proposal.synonyms_fa)
        lines.append("")
    if result.proposal.business_rules:
        lines.extend(["## قواعد کسب‌وکار پیشنهادی", ""])
        for rule in result.proposal.business_rules:
            prefix = f"{rule.rule_id}: " if rule.rule_id else ""
            lines.extend(
                [
                    f"### {prefix}{rule.title}",
                    "",
                    rule.description,
                    "",
                    f"شاهد: `{rule.evidence[:240]}`" if rule.evidence else "شاهد: ثبت نشده",
                    "",
                ]
            )
    if result.proposal.questions_fa:
        lines.extend(["## سؤال‌های نمونه", ""])
        lines.extend(f"- {item}" for item in result.proposal.questions_fa)
        lines.append("")
    if result.proposal.ambiguity_notes_fa:
        lines.extend(["## ابهام‌های نیازمند تصمیم انسانی", ""])
        lines.extend(f"- {item}" for item in result.proposal.ambiguity_notes_fa)
        lines.append("")
    lines.extend(["## گزارش کنترل کیفیت", ""])
    if result.issues:
        lines.extend(
            f"- **{issue.severity.value.upper()} / {issue.code}:** {issue.message}"
            for issue in result.issues
        )
    else:
        lines.append("- خطای ساختاری شناسایی نشد؛ تأیید انسانی همچنان الزامی است.")
    lines.extend(
        [
            "",
            "## محتوای اصلی حفاظت‌شده",
            "",
            "محتوای زیر عیناً از منبع نگهداری شده و توسط LLM بازنویسی نشده است.",
            "",
            f"```{Path(result.source.filename).suffix.lstrip('.')}",
            result.source.content.rstrip(),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _render_verified_query(result: StandardizationResult, *, status: str) -> str:
    metadata = _metadata(result, status=status)
    metadata.update(
        {
            "intent": result.facts.frontmatter.get("intent"),
            "grain": result.proposal.grain or None,
            "questions": result.proposal.questions_fa,
            "expected_tables": result.facts.tables,
            "quality_issues": [
                {"code": issue.code, "severity": issue.severity.value}
                for issue in result.issues
            ],
        }
    )
    header = json.dumps(metadata, ensure_ascii=False, indent=2)
    return (
        "/* STANDARDIZER_METADATA\n"
        + header
        + "\nEND_STANDARDIZER_METADATA */\n\n"
        + result.source.content.rstrip()
        + "\n"
    )


def render_manifest(results: list[StandardizationResult]) -> str:
    payload = {
        "knowledge_base": {
            "name": "municipality_nl2sql",
            "version": "1.0.0",
            "language": "fa-IR",
            "database_dialect": "tsql",
            "default_schema": "dbo",
        },
        "generation_policy": {
            "allowed_statuses": ["approved"],
            "pending_statuses": ["draft"],
            "fail_on_conflict": True,
            "human_approval_required": True,
        },
        "security": {
            "read_only": True,
            "maximum_rows": 100,
            "default_rows": 20,
            "query_timeout_seconds": 10,
            "prohibited_operations": [
                "INSERT",
                "UPDATE",
                "DELETE",
                "MERGE",
                "DROP",
                "ALTER",
                "TRUNCATE",
                "EXEC",
            ],
        },
        "documents": [
            {
                "document_id": result.id,
                "path": result.output_relative_path,
                "doc_type": result.proposal.doc_type.value,
                "status": result.review_status.value,
                "quality_score": result.quality_score,
                "source_hash": result.source.content_hash,
                "blocked": result.is_blocked,
            }
            for result in results
        ],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def render_review_queue(results: list[StandardizationResult]) -> str:
    rows = []
    for result in results:
        if result.review_status.value == "pending":
            rows.append(
                json.dumps(
                    {
                        "id": result.id,
                        "source_file": result.source.filename,
                        "output_path": result.output_relative_path,
                        "doc_type": result.proposal.doc_type.value,
                        "quality_score": result.quality_score,
                        "blocked": result.is_blocked,
                        "issues": [issue.model_dump(mode="json") for issue in result.issues],
                    },
                    ensure_ascii=False,
                )
            )
    return "\n".join(rows) + ("\n" if rows else "")
