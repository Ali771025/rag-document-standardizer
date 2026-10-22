from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from standardizer.models import ColumnFact, ObservedFacts, Relationship

_IDENTIFIER = r"(?:\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$#@]*)"
_QUALIFIED = rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?"
_PROHIBITED = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE", "EXEC", "CREATE LOGIN")


def clean_identifier(value: str) -> str:
    parts = [part.strip().strip("[]`\"") for part in re.split(r"\s*\.\s*", value.strip())]
    return ".".join(part for part in parts if part)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", content, flags=re.DOTALL)
    if not match:
        return {}, content
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, content[match.end() :]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _extract_questions(content: str) -> list[str]:
    questions: list[str] = []
    for line in content.splitlines():
        value = re.sub(r"^\s*(?:--|/\*+|\*|#|-)\s*", "", line).strip()
        if re.match(r"(?i)^(?:question|سؤال|سوال)\s*:", value):
            questions.append(value.split(":", 1)[1].strip())
        elif value.endswith(("؟", "?")) and len(value) >= 8:
            questions.append(value)
    return _dedupe(questions)


def _extract_comment_metadata(content: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    allowed = {
        "document_id",
        "doc_type",
        "status",
        "dialect",
        "domain",
        "intent",
        "grain",
        "validated_by",
        "last_reviewed",
        "table",
        "schema",
        "database",
    }
    for line in content.splitlines()[:100]:
        value = re.sub(r"^\s*(?:--|/\*+|\*)\s*", "", line).strip().rstrip("*/ ")
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+)$", value)
        if match and match.group(1).lower() in allowed:
            metadata[match.group(1).lower()] = match.group(2).strip()
    return metadata


def _iter_create_table_bodies(content: str) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    pattern = re.compile(rf"(?is)\bCREATE\s+TABLE\s+(?P<table>{_QUALIFIED})\s*\(")
    for match in pattern.finditer(content):
        depth = 1
        index = match.end()
        quote: str | None = None
        while index < len(content) and depth:
            char = content[index]
            if quote:
                if char == quote:
                    if index + 1 < len(content) and content[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth == 0:
            results.append(
                (
                    clean_identifier(match.group("table")),
                    content[match.end() : index - 1],
                )
            )
    return results


def _extract_create_columns(content: str, tables: list[str]) -> list[ColumnFact]:
    columns: list[ColumnFact] = []
    for table, body in _iter_create_table_bodies(content):
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or re.match(
                r"(?i)^(CONSTRAINT|PRIMARY\s+KEY|FOREIGN\s+KEY|REFERENCES|UNIQUE|CHECK|INDEX)\b",
                line,
            ):
                continue
            column_match = re.match(
                rf"(?is)^(?P<column>{_IDENTIFIER})\s+"
                r"(?P<type>[A-Za-z][A-Za-z0-9_]*(?:\s*\([^)]*\))?)"
                r"(?P<rest>.*)$",
                line,
            )
            if not column_match:
                continue
            rest = column_match.group("rest")
            nullable = None
            if re.search(r"(?i)\bNOT\s+NULL\b", rest):
                nullable = False
            elif re.search(r"(?i)\bNULL\b", rest):
                nullable = True
            columns.append(
                ColumnFact(
                    table=table,
                    name=clean_identifier(column_match.group("column")),
                    data_type=re.sub(r"\s+", "", column_match.group("type")),
                    nullable=nullable,
                )
            )
        if table not in tables:
            tables.append(table)
    return columns


def _extract_relationships(content: str) -> list[Relationship]:
    relationships: list[Relationship] = []
    fk_pattern = re.compile(
        rf"(?is)(?:ALTER\s+TABLE\s+(?P<alter_table>{_QUALIFIED}).*?)?"
        rf"FOREIGN\s+KEY\s*\(\s*(?P<src_col>{_IDENTIFIER})\s*\)"
        rf"\s+REFERENCES\s+(?P<target_table>{_QUALIFIED})"
        rf"\s*\(\s*(?P<target_col>{_IDENTIFIER})\s*\)"
    )
    create_table_match = re.search(rf"(?is)\bCREATE\s+TABLE\s+(?P<table>{_QUALIFIED})", content)
    default_source = clean_identifier(create_table_match.group("table")) if create_table_match else ""
    for match in fk_pattern.finditer(content):
        source_table = clean_identifier(match.group("alter_table") or default_source)
        relationships.append(
            Relationship(
                source_table=source_table,
                source_column=clean_identifier(match.group("src_col")),
                target_table=clean_identifier(match.group("target_table")),
                target_column=clean_identifier(match.group("target_col")),
                cardinality="N:1",
                join_type="LEFT",
                evidence=match.group(0)[:500],
            )
        )

    join_pattern = re.compile(
        rf"(?is)\b(?P<join_type>LEFT|INNER|RIGHT|FULL)?\s*JOIN\s+(?P<target>{_QUALIFIED})"
        rf"(?:\s+(?:AS\s+)?(?P<alias>{_IDENTIFIER}))?\s+ON\s+"
        rf"(?P<left>{_IDENTIFIER})\.(?P<left_col>{_IDENTIFIER})\s*=\s*"
        rf"(?P<right>{_IDENTIFIER})\.(?P<right_col>{_IDENTIFIER})"
    )
    alias_map = _extract_alias_map(content)
    for match in join_pattern.finditer(content):
        left_alias = clean_identifier(match.group("left"))
        right_alias = clean_identifier(match.group("right"))
        target = clean_identifier(match.group("target"))
        target_alias = clean_identifier(match.group("alias") or target.split(".")[-1])
        alias_map.setdefault(target_alias.casefold(), target)
        left_table = alias_map.get(left_alias.casefold(), left_alias)
        right_table = alias_map.get(right_alias.casefold(), right_alias)
        left_column = clean_identifier(match.group("left_col"))
        right_column = clean_identifier(match.group("right_col"))
        if left_column.casefold() == "id" and right_column.casefold().endswith("id"):
            source_table, source_column = right_table, right_column
            target_table, target_column = left_table, left_column
            cardinality = "N:1"
        else:
            source_table, source_column = left_table, left_column
            target_table, target_column = right_table, right_column
            cardinality = "unknown"
        relationships.append(
            Relationship(
                source_table=source_table,
                source_column=source_column,
                target_table=target_table,
                target_column=target_column,
                cardinality=cardinality,
                join_type=(match.group("join_type") or "INNER").upper(),
                evidence=match.group(0)[:500],
            )
        )
    unique: dict[tuple[str, str, str, str], Relationship] = {}
    for relation in relationships:
        key = (
            relation.source_table.casefold(),
            relation.source_column.casefold(),
            relation.target_table.casefold(),
            relation.target_column.casefold(),
        )
        unique.setdefault(key, relation)
    return list(unique.values())


def _extract_alias_map(content: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    pattern = re.compile(
        rf"(?is)\b(?:FROM|JOIN)\s+(?P<table>{_QUALIFIED})"
        rf"(?:\s+(?:AS\s+)?(?P<alias>{_IDENTIFIER}))?"
    )
    reserved = {"ON", "WHERE", "LEFT", "RIGHT", "INNER", "FULL", "JOIN", "ORDER", "GROUP", "CROSS"}
    for match in pattern.finditer(content):
        table = clean_identifier(match.group("table"))
        alias = clean_identifier(match.group("alias") or table.split(".")[-1])
        if alias.upper() in reserved:
            alias = table.split(".")[-1]
        alias_map[alias.casefold()] = table
        alias_map[table.split(".")[-1].casefold()] = table
    return alias_map


def _extract_tables(content: str) -> list[str]:
    tables: list[str] = []
    patterns = (
        rf"(?is)\bCREATE\s+TABLE\s+({_QUALIFIED})",
        rf"(?is)\bALTER\s+TABLE\s+({_QUALIFIED})",
        rf"(?is)\b(?:FROM|JOIN|UPDATE|INTO)\s+({_QUALIFIED})",
        rf"(?is)\bREFERENCES\s+({_QUALIFIED})",
    )
    reserved = {"SELECT", "VALUES", "OPENJSON"}
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            table = clean_identifier(match.group(1))
            if table.upper() not in reserved:
                tables.append(table)
    return _dedupe(tables)


def parse_sql(content: str) -> ObservedFacts:
    tables = _extract_tables(content)
    columns = _extract_create_columns(content, tables)
    relationships = _extract_relationships(content)
    metadata = _extract_comment_metadata(content)
    prohibited = [operation for operation in _PROHIBITED if re.search(rf"(?i)\b{operation}\b", content)]
    statements = []
    for keyword in ("CREATE TABLE", "ALTER TABLE", "SELECT", "WITH", "INSERT", "UPDATE", "DELETE"):
        if re.search(rf"(?i)\b{keyword.replace(' ', r'\s+')}\b", content):
            statements.append(keyword)
    evidence: list[str] = list(tables)
    evidence.extend(column.name for column in columns)
    for relation in relationships:
        evidence.extend(
            [
                relation.source_table,
                relation.source_column,
                relation.target_table,
                relation.target_column,
            ]
        )
    evidence.extend(
        clean_identifier(match.group(1) or match.group(2))
        for match in re.finditer(r"(?:\[([^\]]+)\]|`([^`]+)`)", content)
        if (match.group(1) or match.group(2))
    )
    parse_errors: list[str] = []
    try:
        import sqlglot

        sqlglot.parse(content, read="tsql")
    except ImportError:
        pass
    except Exception as exc:
        parse_errors.append(str(exc)[:500])
    return ObservedFacts(
        tables=tables,
        columns=columns,
        relationships=relationships,
        statements=statements,
        frontmatter=metadata,
        questions=_extract_questions(content),
        identifier_evidence=_dedupe(evidence),
        has_select_star=bool(re.search(r"(?is)\bSELECT\s+(?:TOP\s*\([^)]*\)\s+)?\*", content)),
        prohibited_operations=prohibited,
        parse_errors=parse_errors,
    )


def parse_markdown(content: str) -> ObservedFacts:
    frontmatter, body = parse_frontmatter(content)
    evidence: list[str] = []
    for match in re.finditer(r"`([^`\n]+)`", body):
        value = clean_identifier(match.group(1))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#@]*(?:\.[A-Za-z_][A-Za-z0-9_$#@]*)?", value):
            evidence.append(value)
            evidence.extend(value.split("."))
    sql_blocks = re.findall(r"(?is)```sql\s*(.*?)```", body)
    combined_sql = "\n".join(sql_blocks)
    sql_facts = parse_sql(combined_sql) if combined_sql else ObservedFacts()
    relationships = list(sql_facts.relationships)
    arrow_pattern = re.compile(
        r"(?m)^\s*[-*]?\s*`?(?P<src_table>[A-Za-z_][A-Za-z0-9_]*)`?\."
        r"`?(?P<src_col>[A-Za-z_][A-Za-z0-9_]*)`?\s*(?:→|->)\s*"
        r"`?(?P<dst_table>[A-Za-z_][A-Za-z0-9_]*)`?\."
        r"`?(?P<dst_col>[A-Za-z_][A-Za-z0-9_]*)`?"
    )
    for match in arrow_pattern.finditer(body):
        relationships.append(
            Relationship(
                source_table=match.group("src_table"),
                source_column=match.group("src_col"),
                target_table=match.group("dst_table"),
                target_column=match.group("dst_col"),
                evidence=match.group(0),
            )
        )
    tables = _dedupe(
        list(sql_facts.tables)
        + [
            value
            for value in evidence
            if "." not in value
            and (
                value.lower().startswith(("tinf", "tbas", "tbl", "dim", "fact"))
                or value == frontmatter.get("table")
            )
        ]
    )
    columns = list(sql_facts.columns)
    return ObservedFacts(
        tables=tables,
        columns=columns,
        relationships=relationships,
        statements=sql_facts.statements,
        frontmatter=frontmatter,
        questions=_extract_questions(body),
        identifier_evidence=_dedupe(evidence + sql_facts.identifier_evidence),
        has_select_star=sql_facts.has_select_star,
        prohibited_operations=sql_facts.prohibited_operations,
        parse_errors=sql_facts.parse_errors,
    )


def parse_document(filename: str, content: str) -> ObservedFacts:
    suffix = Path(filename).suffix.lower()
    if suffix == ".sql":
        return parse_sql(content)
    if suffix == ".md":
        return parse_markdown(content)
    raise ValueError("Only .sql and .md files are supported")
