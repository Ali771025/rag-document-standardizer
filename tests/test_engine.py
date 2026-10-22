from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from standardizer.config import Settings
from standardizer.engine import StandardizationEngine, StandardizationError
from standardizer.llm import ProposalProvider
from standardizer.models import (
    DocumentType,
    ObservedFacts,
    Relationship,
    ReviewStatus,
    SourceDocument,
    StandardizationProposal,
)


DDL = """
CREATE TABLE dbo.TINFCommission100
(
    ID bigint NOT NULL,
    Number nvarchar(50) NULL,
    FRegisterDate datetime NULL,
    TBASCommission100StateID bigint NULL,
    CONSTRAINT PK_TINFCommission100 PRIMARY KEY (ID),
    CONSTRAINT FK_C100_State FOREIGN KEY (TBASCommission100StateID)
        REFERENCES dbo.TBASCommission100State(ID)
);
"""


class HallucinatingProvider(ProposalProvider):
    name = "test"
    model = "hallucinating"

    def propose(
        self,
        source: SourceDocument,
        facts: ObservedFacts,
        *,
        default_domain: str,
    ) -> StandardizationProposal:
        _ = (source, facts)
        return StandardizationProposal(
            doc_type=DocumentType.RELATIONSHIP,
            title_fa="رابطه آزمایشی",
            domain=default_domain,
            summary_fa="آزمون جلوگیری از شناسه خیالی",
            canonical_entity="TINFCommission100",
            relationships=[
                Relationship(
                    source_table="ImaginaryTable",
                    source_column="ImaginaryColumn",
                    target_table="AnotherFakeTable",
                    target_column="FakeID",
                )
            ],
        )


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = Settings(
            output_dir=root / "output",
            db_path=root / "standardizer.db",
            llm_enabled=False,
        )
        self.engine = StandardizationEngine(settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_table_schema_is_parsed_and_rendered(self) -> None:
        result = self.engine.standardize_text("TINFCommission100.sql", DDL, use_llm=False)
        self.assertEqual(result.proposal.doc_type, DocumentType.TABLE_SCHEMA)
        self.assertIn("dbo.TINFCommission100", result.facts.tables)
        self.assertIn("Number", {column.name for column in result.facts.columns})
        self.assertEqual(len(result.facts.relationships), 1)
        self.assertTrue((self.engine.settings.output_dir / result.output_relative_path).exists())
        self.assertIn("status: draft", result.rendered_content)

    def test_select_star_is_blocked(self) -> None:
        result = self.engine.standardize_text(
            "unsafe.sql",
            "SELECT * FROM dbo.TINFCommission100;",
            use_llm=False,
        )
        self.assertTrue(result.is_blocked)
        self.assertIn("SELECT_STAR", {issue.code for issue in result.issues})

    def test_hallucinated_identifiers_are_blocked(self) -> None:
        root = Path(self.temp.name)
        engine = StandardizationEngine(
            Settings(
                output_dir=root / "hallucination-output",
                db_path=root / "hallucination.db",
                llm_enabled=True,
            ),
            provider=HallucinatingProvider(),
        )
        result = engine.standardize_text("source.sql", DDL)
        self.assertTrue(result.is_blocked)
        self.assertIn("UNGROUNDED_IDENTIFIER", {issue.code for issue in result.issues})

    def test_id_is_stable_for_same_source(self) -> None:
        first = self.engine.standardize_text("same.sql", DDL, use_llm=False)
        second = self.engine.standardize_text("same.sql", DDL, use_llm=False)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.output_relative_path, second.output_relative_path)

    def test_approval_updates_document_status(self) -> None:
        result = self.engine.standardize_text("table.sql", DDL, use_llm=False)
        approved = self.engine.review(
            result.id,
            decision=ReviewStatus.APPROVED,
            reviewer="database-team",
            note="DDL verified against SQL Server.",
        )
        self.assertEqual(approved.review_status, ReviewStatus.APPROVED)
        self.assertIn("status: approved", approved.rendered_content)
        loaded = self.engine.store.get(result.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.review_status, ReviewStatus.APPROVED)
        rerun = self.engine.standardize_text("table.sql", DDL, use_llm=False)
        self.assertEqual(rerun.review_status, ReviewStatus.APPROVED)

    def test_blocked_document_cannot_be_approved(self) -> None:
        result = self.engine.standardize_text(
            "unsafe.sql",
            "DROP TABLE dbo.TINFCommission100;",
            use_llm=False,
        )
        with self.assertRaises(StandardizationError):
            self.engine.review(
                result.id,
                decision=ReviewStatus.APPROVED,
                reviewer="reviewer",
            )

    def test_batch_writes_manifest_and_review_queue(self) -> None:
        root = Path(self.temp.name)
        input_dir = root / "input"
        input_dir.mkdir()
        (input_dir / "table.sql").write_text(DDL, encoding="utf-8")
        report = self.engine.standardize_directory(
            input_dir,
            output_directory=root / "batch-output",
            use_llm=False,
        )
        self.assertEqual(report.standardized, 1)
        self.assertTrue((root / "batch-output" / "manifest.yaml").exists())
        self.assertTrue((root / "batch-output" / "review_queue.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
