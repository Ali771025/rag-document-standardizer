from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from standardizer.config import Settings
from standardizer.engine import StandardizationEngine, StandardizationError
from standardizer.models import ReviewStatus


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "output", None):
        settings = replace(settings, output_dir=Path(args.output))
    if getattr(args, "db", None):
        settings = replace(settings, db_path=Path(args.db))
    if getattr(args, "model", None):
        settings = replace(settings, ollama_model=args.model)
    return settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-standardizer",
        description="موتور استانداردسازی اسناد SQL/Markdown برای RAG",
    )
    parser.add_argument("--db", help="مسیر SQLite صف بازبینی")
    parser.add_argument("--model", help="نام مدل Ollama")
    sub = parser.add_subparsers(dest="command", required=True)

    standardize = sub.add_parser("standardize", help="استانداردسازی یک پوشه")
    standardize.add_argument("--input", required=True, help="پوشه فایل‌های .md و .sql")
    standardize.add_argument("--output", required=True, help="پوشه خروجی استاندارد")
    standardize.add_argument("--no-llm", action="store_true", help="اجرای فقط قواعد قطعی")

    validate = sub.add_parser("validate", help="بررسی یک فایل بدون نوشتن خروجی")
    validate.add_argument("--input", required=True)
    validate.add_argument("--no-llm", action="store_true")

    doctor = sub.add_parser("doctor", help="بررسی اتصال و مدل Ollama")
    doctor.add_argument("--output")

    review = sub.add_parser("review", help="نمایش صف بازبینی")
    review.add_argument(
        "--status",
        choices=[item.value for item in ReviewStatus],
        default="pending",
    )
    review.add_argument("--limit", type=int, default=100)

    decide = sub.add_parser("decide", help="تأیید یا رد سند")
    decide.add_argument("result_id")
    decide.add_argument("--decision", choices=["approved", "rejected"], required=True)
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--note", default="")
    decide.add_argument("--output", required=True)

    serve = sub.add_parser("serve", help="اجرای API و پنل وب")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8088, type=int)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = _settings_from_args(args)
    try:
        if args.command == "serve":
            import uvicorn

            uvicorn.run("standardizer.api:app", host=args.host, port=args.port, reload=False)
            return
        engine = StandardizationEngine(settings)
        if args.command == "standardize":
            report = engine.standardize_directory(
                Path(args.input),
                output_directory=Path(args.output),
                use_llm=not args.no_llm,
            )
            print(
                json.dumps(
                    {
                        "batch_id": report.batch_id,
                        "total": report.total,
                        "standardized": report.standardized,
                        "skipped": report.skipped,
                        "blocked": report.blocked,
                        "pending_review": report.pending_review,
                        "average_quality_score": report.average_quality_score,
                        "output_directory": report.output_directory,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "validate":
            path = Path(args.input)
            result = engine.standardize_text(
                path.name,
                path.read_text(encoding="utf-8-sig"),
                source_path=str(path),
                use_llm=not args.no_llm,
                write_output=False,
            )
            print(result.model_dump_json(indent=2))
            sys.exit(2 if result.is_blocked else 0)
        if args.command == "doctor":
            health = engine.health()
            print(json.dumps(health, ensure_ascii=False, indent=2))
            sys.exit(0 if health["llm"].get("ok", False) else 1)
        if args.command == "review":
            results = engine.store.list(
                status=ReviewStatus(args.status),
                limit=args.limit,
            )
            print(
                json.dumps(
                    [
                        {
                            "id": result.id,
                            "source_file": result.source.filename,
                            "doc_type": result.proposal.doc_type.value,
                            "quality_score": result.quality_score,
                            "blocked": result.is_blocked,
                            "output_path": result.output_relative_path,
                        }
                        for result in results
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "decide":
            result = engine.review(
                args.result_id,
                decision=ReviewStatus(args.decision),
                reviewer=args.reviewer,
                note=args.note,
            )
            print(result.model_dump_json(indent=2))
            return
    except (OSError, UnicodeError, StandardizationError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
