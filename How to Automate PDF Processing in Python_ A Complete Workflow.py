import csv
import logging
import os
import pathlib
import sys
import tempfile
import time

import aspose.pdf as apdf
from dotenv import dotenv_values


MAX_PDF_BYTES = 1_000_000_000  # 1 GB; reject larger inputs before loading them.
REPORT_COLUMNS = ("input_file", "output_file", "status", "processing_seconds", "error")


class SkipPDF(Exception):
    """The input cannot be processed under the batch's safety policy."""


def optimize(input_file: str, output_file: str) -> None:
    try:
        document = apdf.Document(input_file)
    except RuntimeError as exc:
        # Aspose's Python bridge exposes .NET exceptions as RuntimeError.
        if "InvalidPasswordException" in str(exc):
            raise SkipPDF("Password-protected PDF; no password supplied") from exc
        if "InvalidPdfFileFormatException" in str(exc):
            raise ValueError(f"Invalid or corrupted PDF: {exc}") from exc
        raise
    try:
        if document.is_encrypted:
            raise SkipPDF("Encrypted PDF; password-protected files are skipped")
        document.optimize()
        document.save(output_file)
    finally:
        del document


def configure_logging(logs_folder: pathlib.Path) -> None:
    logs_folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(logs_folder / "processing.log"),
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def find_pdf_files(input_folder: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        path
        for path in input_folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )


def process_file(input_file: pathlib.Path, output_file: pathlib.Path) -> tuple[str, float, str]:
    """Optimize one PDF, recording failures without interrupting the batch."""
    started_at = time.perf_counter()
    logging.info("Processing %s", input_file.name)
    error = ""
    try:
        if os.path.lexists(output_file):
            raise SkipPDF("Output already exists; left unchanged")
        if input_file.stat().st_size > MAX_PDF_BYTES:
            raise SkipPDF("Input exceeds the 1 GB size limit")
        with input_file.open("rb") as source:
            if b"%PDF-" not in source.read(1024):
                raise ValueError("Invalid PDF: missing PDF header")
        # Publish only complete PDFs. Hard-link creation cannot overwrite an
        # output created by another process after the existence check.
        with tempfile.TemporaryDirectory(prefix=".pdf-", dir=output_file.parent) as work:
            temporary_output = pathlib.Path(work) / "optimized.pdf"
            optimize(str(input_file), str(temporary_output))
            os.link(temporary_output, output_file)
    except (SkipPDF, FileExistsError) as exc:
        status = "skipped"
        error = str(exc)
        logging.warning("Skipped %s: %s", input_file.name, error)
    except PermissionError as exc:
        status = "failed"
        error = f"Permission denied reading input or writing output: {exc}"
        logging.error("Failed to process %s: %s", input_file.name, error)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        logging.exception("Failed to process %s", input_file.name)
    else:
        status = "success"
        logging.info("Successfully processed %s", input_file.name)
    return status, time.perf_counter() - started_at, error


def process_files(
    input_files: list[pathlib.Path],
    output_folder: pathlib.Path,
    report_path: pathlib.Path,
) -> tuple[int, int, int]:
    """Write a report and return success, failure, and skipped counts."""
    if input_files:
        output_folder.mkdir(parents=True, exist_ok=True)

    successfully_processed = 0
    failed = 0
    skipped = 0
    logging.info("Files found: %d", len(input_files))

    with report_path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(REPORT_COLUMNS)
        report_file.flush()

        for pdf_in_file in input_files:
            pdf_out_file = output_folder / pdf_in_file.name
            status, processing_seconds, error = process_file(pdf_in_file, pdf_out_file)
            if status == "success":
                successfully_processed += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1

            writer.writerow(
                [
                    str(pdf_in_file),
                    str(pdf_out_file),
                    status,
                    f"{processing_seconds:.3f}",
                    error,
                ]
            )
            report_file.flush()

    return successfully_processed, failed, skipped


def main(
    base_folder: pathlib.Path | None = None,
    license_path: str | None = None,
) -> int:
    started_at = time.perf_counter()
    config_path = pathlib.Path(__file__).resolve().parent / ".env"
    with config_path.open(encoding="utf-8") as config_file:
        config = dotenv_values(stream=config_file)
    if base_folder is None:
        configured_base_folder = config.get("BASE_FOLDER")
        if not configured_base_folder or not configured_base_folder.strip():
            raise ValueError("BASE_FOLDER must be set in .env")
        base_folder = pathlib.Path(configured_base_folder).expanduser()
        if not base_folder.is_absolute():
            base_folder = config_path.parent / base_folder
    if license_path is None:
        configured_license_path = config.get("LICENSE_PATH")
        if not configured_license_path or not configured_license_path.strip():
            raise ValueError("LICENSE_PATH must be set in .env")
        license_file = pathlib.Path(configured_license_path).expanduser()
        if not license_file.is_absolute():
            license_file = config_path.parent / license_file
        license_path = str(license_file)
    if not base_folder.is_dir():
        raise NotADirectoryError(f"BASE_FOLDER is not a directory: {base_folder}")
    if not pathlib.Path(license_path).is_file():
        raise FileNotFoundError(f"LICENSE_PATH is not a file: {license_path}")
    logs_folder = base_folder / "logs"
    report_path = logs_folder / "processing-report.csv"
    try:
        configure_logging(logs_folder)
        logging.info("Processing started")
        pdf_in_files = find_pdf_files(base_folder / "input")
        if pdf_in_files:
            pdf_license = apdf.License()
            pdf_license.set_license(license_path)
        else:
            logging.info("No PDF files found; nothing to process")
            print("No PDF files found; nothing to process.")
        successfully_processed, failed, skipped = process_files(
            pdf_in_files, base_folder / "output", report_path
        )
    except (OSError, RuntimeError) as exc:
        message = f"Batch could not complete: {exc}"
        logging.error(message)
        print(message, file=sys.stderr)
        raise

    total_seconds = time.perf_counter() - started_at
    summary = (
        f"Files found:            {len(pdf_in_files)}\n"
        f"Successfully processed: {successfully_processed}\n"
        f"Failed:                 {failed}\n"
        f"Skipped:                {skipped}\n"
        f"Total processing time: {total_seconds:.1f} seconds\n"
        f"Report: {report_path.relative_to(base_folder).as_posix()}"
    )
    logging.info("Processing finished\n%s", summary)
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

