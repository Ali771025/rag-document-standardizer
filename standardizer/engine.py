from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from standardizer.config import Settings
from standardizer.llm import DeterministicProvider, LLMError, OllamaProvider, ProposalProvider
from standardizer.models import (
    BatchReport,
    ReviewStatus,
    Severity,
    SourceDocument,
    StandardizationResult,
)
from standardizer.parsers import parse_document
from standardizer.renderer import (
    output_path_for,
    render_manifest,
    render_result,
    render_review_queue,
)
from standardizer.storage import ResultStore
from standardizer.validators import detect_batch_conflicts, validate_proposal


class StandardizationError(RuntimeError):
    pass


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


class StandardizationEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: ProposalProvider | None = None,
        store: ResultStore | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.deterministic = DeterministicProvider()
        self.provider = provider or (
            OllamaProvider(self.settings) if self.settings.llm_enabled else self.deterministic
        )
        self.store = store or ResultStore(self.settings.db_path)

    @staticmethod
    def make_source(filename: str, content: str, source_path: str = "") -> SourceDocument:
        safe_filename = Path(filename).name
        suffix = Path(safe_filename).suffix.lower()
        if suffix not in {".md", ".sql"}:
            raise StandardizationError("فقط فایل‌های .md و .sql پشتیبانی می‌شوند.")
        if not content.strip():
            raise StandardizationError("فایل ورودی خالی است.")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return SourceDocument(
            filename=safe_filename,
            content=content,
            source_path=source_path,
            content_hash=content_hash,
        )

    def health(self) -> dict[str, object]:
        llm = self.provider.health()
        return {
            "ok": True,
            "version": "1.0.0",
            "llm": llm,
            "output_dir": str(self.settings.output_dir),
            "database": str(self.settings.db_path),
        }

    def standardize_text(
        self,
        filename: str,
        content: str,
        *,
        source_path: str = "",
        use_llm: bool | None = None,
        write_output: bool = True,
        refresh_indexes: bool = True,
    ) -> StandardizationResult:
        if len(content.encode("utf-8")) > self.settings.max_file_bytes:
            raise StandardizationError(
                f"حجم فایل از سقف {self.settings.max_file_bytes} بایت بیشتر است."
            )
        source = self.make_source(filename, content, source_path)
        result_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"rag-standardizer:{source.filename}:{source.content_hash}",
            )
        )
        existing_approved = self.store.get(result_id)
        if (
            existing_approved is not None
            and existing_approved.review_status == ReviewStatus.APPROVED
        ):
            if write_output:
                _atomic_write(
                    self.settings.output_dir / existing_approved.output_relative_path,
                    existing_approved.rendered_content,
                )
                if refresh_indexes:
                    self._refresh_indexes()
            return existing_approved
        facts = parse_document(filename, content)
        llm_failed: str | None = None
        selected_provider: ProposalProvider
        if use_llm is False:
            selected_provider = self.deterministic
        elif use_llm is True and isinstance(self.provider, DeterministicProvider):
            selected_provider = OllamaProvider(self.settings)
        else:
            selected_provider = self.provider
        try:
            proposal = selected_provider.propose(
                source,
                facts,
                default_domain=self.settings.default_domain,
            )
        except LLMError as exc:
            llm_failed = str(exc)
            selected_provider = self.deterministic
            proposal = selected_provider.propose(
                source,
                facts,
                default_domain=self.settings.default_domain,
            )
        issues, score = validate_proposal(
            source,
            facts,
            proposal,
            llm_failed=llm_failed,
        )
        initial = StandardizationResult(
            id=result_id,
            source=source,
            facts=facts,
            proposal=proposal,
            issues=issues,
            quality_score=score,
            output_relative_path="pending",
            rendered_content="",
            llm_provider=selected_provider.name,
            llm_model=selected_provider.model,
        )
        relative_path = output_path_for(initial)
        result = initial.model_copy(update={"output_relative_path": relative_path})
        existing = self.store.get(result.id)
        if existing is not None:
            result.review_status = existing.review_status
        render_status = (
            "approved"
            if result.review_status == ReviewStatus.APPROVED
            else "rejected"
            if result.review_status == ReviewStatus.REJECTED
            else "draft"
        )
        result.rendered_content = render_result(result, status=render_status)
        self.store.save(result)
        if write_output:
            _atomic_write(self.settings.output_dir / relative_path, result.rendered_content)
            if refresh_indexes:
                self._refresh_indexes()
        return result

    def standardize_directory(
        self,
        input_directory: Path,
        *,
        output_directory: Path | None = None,
        use_llm: bool | None = None,
    ) -> BatchReport:
        input_directory = input_directory.resolve()
        if not input_directory.is_dir():
            raise StandardizationError(f"مسیر ورودی پوشه نیست: {input_directory}")
        original_output = self.settings.output_dir
        if output_directory is not None:
            self.settings = replace(self.settings, output_dir=output_directory.resolve())
        try:
            files = sorted(
                path
                for path in input_directory.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in {".md", ".sql"}
                and self.settings.output_dir.resolve() not in path.resolve().parents
            )
            results: list[StandardizationResult] = []
            for path in files:
                if path.stat().st_size > self.settings.max_file_bytes:
                    continue
                try:
                    content = path.read_text(encoding="utf-8-sig")
                except UnicodeDecodeError:
                    content = path.read_text(encoding="cp1256")
                results.append(
                    self.standardize_text(
                        path.name,
                        content,
                        source_path=str(path),
                        use_llm=use_llm,
                        write_output=True,
                        refresh_indexes=False,
                    )
                )
            conflicts = detect_batch_conflicts(results)
            for index, result in enumerate(results):
                extra = conflicts.get(result.id, [])
                if not extra:
                    continue
                deductions = sum(
                    35 if issue.severity == Severity.BLOCKER else 18 for issue in extra
                )
                updated = result.model_copy(
                    update={
                        "issues": result.issues + extra,
                        "quality_score": max(0, result.quality_score - deductions),
                    }
                )
                updated.rendered_content = render_result(updated)
                results[index] = updated
                self.store.save(updated)
                _atomic_write(
                    self.settings.output_dir / updated.output_relative_path,
                    updated.rendered_content,
                )
            batch_id = str(uuid.uuid4())
            report = BatchReport(
                batch_id=batch_id,
                input_directory=str(input_directory),
                output_directory=str(self.settings.output_dir.resolve()),
                total=len(files),
                standardized=len(results),
                skipped=len(files) - len(results),
                blocked=sum(result.is_blocked for result in results),
                pending_review=sum(
                    result.review_status == ReviewStatus.PENDING for result in results
                ),
                approved=sum(
                    result.review_status == ReviewStatus.APPROVED for result in results
                ),
                average_quality_score=(
                    round(sum(result.quality_score for result in results) / len(results), 2)
                    if results
                    else 0.0
                ),
                results=results,
            )
            _atomic_write(self.settings.output_dir / "manifest.yaml", render_manifest(results))
            _atomic_write(
                self.settings.output_dir / "review_queue.jsonl",
                render_review_queue(results),
            )
            _atomic_write(
                self.settings.output_dir / "batch_report.json",
                json.dumps(
                    report.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            return report
        finally:
            if output_directory is not None and self.settings.output_dir != original_output:
                self.settings = replace(self.settings, output_dir=original_output)

    def review(
        self,
        result_id: str,
        *,
        decision: ReviewStatus,
        reviewer: str,
        note: str = "",
    ) -> StandardizationResult:
        result = self.store.get(result_id)
        if result is None:
            raise StandardizationError("رکورد استانداردسازی پیدا نشد.")
        if decision == ReviewStatus.APPROVED and result.is_blocked:
            raise StandardizationError(
                "سند دارای خطای blocker است و پیش از تأیید باید منبع یا رابطه اصلاح شود."
            )
        if decision == ReviewStatus.PENDING:
            raise StandardizationError("تصمیم بازبینی نمی‌تواند pending باشد.")
        result.review_status = decision
        render_status = "approved" if decision == ReviewStatus.APPROVED else "rejected"
        result.rendered_content = render_result(result, status=render_status)
        self.store.save(result)
        self.store.decide(result.id, decision, reviewer, note)
        _atomic_write(
            self.settings.output_dir / result.output_relative_path,
            result.rendered_content,
        )
        self._refresh_indexes()
        return result

    def _refresh_indexes(self) -> None:
        all_results = self.store.list_all()
        _atomic_write(self.settings.output_dir / "manifest.yaml", render_manifest(all_results))
        _atomic_write(
            self.settings.output_dir / "review_queue.jsonl",
            render_review_queue(all_results),
        )
