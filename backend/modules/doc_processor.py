"""
Unified Document Processor
---------------------------
Extracts text from any supported file type and returns the same
chunk format as pdf_processor.py:
    [{"text": str, "page_start": int, "page_end": int}, ...]

Supported formats
-----------------
  .pdf            — PyMuPDF  (existing pipeline, unchanged)
  .docx           — python-docx  (paragraphs + tables)
  .txt  .md       — plain text read directly
  .pptx           — python-pptx  (slide text + notes)
  .xlsx  .xls     — openpyxl  (cell values per sheet)
  .png  .jpg .jpeg .bmp .tiff .webp
                  — Pillow + pytesseract OCR
                    (graceful fallback if Tesseract binary not installed)

All formats share the same chunk_text() helper from pdf_processor so
chunk size / overlap behaviour is identical for every file type.
"""

import os
import re
from pathlib import Path

from modules.pdf_processor import chunk_text, process_pdf  # reuse chunker + PDF path


# ── File-type routing ─────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt", ".md", ".markdown",
    ".pptx",
    ".xlsx", ".xls",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp",
}


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def is_supported(filename: str) -> bool:
    return extension_of(filename) in SUPPORTED_EXTENSIONS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunks_from_pages(pages: list[dict], chunk_size: int = 1000) -> list[dict]:
    """
    Given a list of {"text": str, "page_number": int} dicts (same as
    pdf_processor.extract_pages_from_pdf), produce the standard chunk list.
    """
    result = []
    for page in pages:
        for text in chunk_text(page["text"], chunk_size=chunk_size):
            result.append({
                "text": text,
                "page_start": page["page_number"],
                "page_end":   page["page_number"],
            })
    return result


def _clean(text: str) -> str:
    """Normalise whitespace."""
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Per-format extractors ─────────────────────────────────────────────────────

def _extract_docx(path: str) -> list[dict]:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(path)
    pages: list[dict] = []
    current_page = 1
    buf: list[str] = []

    # Paragraphs
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            buf.append(text)
        # Detect manual page breaks
        for run in para.runs:
            if run._element.xml.find("w:lastRenderedPageBreak") != -1 or \
               run._element.xml.find("w:pageBreak") != -1:
                if buf:
                    pages.append({"text": _clean("\n".join(buf)), "page_number": current_page})
                    buf = []
                    current_page += 1

    # Tables
    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            buf.append("\n".join(rows))

    if buf:
        pages.append({"text": _clean("\n".join(buf)), "page_number": current_page})

    return pages if pages else [{"text": "No text found in document.", "page_number": 1}]


def _extract_txt(path: str) -> list[dict]:
    """Plain text and Markdown — treat every ~60 lines as a logical page."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    PAGE_LINES = 60
    pages = []
    for i in range(0, max(1, len(lines)), PAGE_LINES):
        block = _clean("".join(lines[i: i + PAGE_LINES]))
        if block:
            pages.append({
                "text": block,
                "page_number": (i // PAGE_LINES) + 1,
            })
    return pages if pages else [{"text": "Empty file.", "page_number": 1}]


def _extract_pptx(path: str) -> list[dict]:
    from pptx import Presentation

    prs = Presentation(path)
    pages = []
    for slide_num, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []

        # Shapes (text boxes, titles, content)
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(shape.text.strip())

        # Speaker notes
        if slide.has_notes_slide:
            notes_text = slide.notes_slide.notes_text_frame.text.strip()
            if notes_text:
                parts.append(f"[Notes] {notes_text}")

        if parts:
            pages.append({
                "text": _clean("\n".join(parts)),
                "page_number": slide_num,
            })

    return pages if pages else [{"text": "No text found in presentation.", "page_number": 1}]


def _extract_xlsx(path: str) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    pages = []
    sheet_num = 0

    for sheet_name in wb.sheetnames:
        sheet_num += 1
        ws = wb[sheet_name]
        rows_text: list[str] = []

        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                rows_text.append(" | ".join(cells))

        if rows_text:
            text = f"[Sheet: {sheet_name}]\n" + "\n".join(rows_text)
            pages.append({"text": _clean(text), "page_number": sheet_num})

    wb.close()
    return pages if pages else [{"text": "No data found in spreadsheet.", "page_number": 1}]


def _extract_image(path: str) -> list[dict]:
    """
    Extract text/content from an image using two strategies in order:

    1. LLM Vision API — send the image to a multimodal model and ask it to
       transcribe and describe all content exhaustively. This works without
       any local binary and produces rich, searchable text.

    2. pytesseract OCR — used as a fallback if the vision API call fails
       (e.g. model doesn't support vision, API key missing, rate limit).

    3. Hard fallback — returns a placeholder so the file is still indexed
       and a meaningful error is surfaced to the user.
    """
    # ── Strategy 1: LLM Vision ───────────────────────────────────────────────
    try:
        from modules.llm_client import vision_chat

        vision_prompt = (
            "You are an expert document analyst. Carefully examine this image and:\n"
            "1. Transcribe ALL text visible in the image exactly as it appears.\n"
            "2. Describe any charts, diagrams, tables, or figures in detail.\n"
            "3. Note any headings, labels, captions, or annotations.\n"
            "4. Include every piece of information visible — do not summarise or skip anything.\n\n"
            "Format your response as plain text, preserving the logical structure of the content."
        )

        text = vision_chat(path, vision_prompt, max_tokens=1500)
        text = _clean(text)
        if text:
            return [{"text": text, "page_number": 1}]
    except Exception as vision_exc:
        # Vision failed — try OCR next
        _vision_error = str(vision_exc)
    else:
        _vision_error = None

    # ── Strategy 2: pytesseract OCR ──────────────────────────────────────────
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        text = pytesseract.image_to_string(img)
        text = _clean(text)
        if text:
            return [{"text": text, "page_number": 1}]
        return [{"text": "[Image uploaded — no text detected by OCR.]", "page_number": 1}]
    except Exception:
        pass

    # ── Strategy 3: hard fallback ────────────────────────────────────────────
    msg = (
        "[Image could not be processed. "
        "To enable image understanding, set LLM_VISION_MODEL in backend/.env "
        "to a vision-capable model such as meta-llama/llama-4-scout-17b-16e-instruct "
        "(available free on Groq). "
        "Alternatively, install Tesseract from https://tesseract-ocr.github.io.]"
    )
    return [{"text": msg, "page_number": 1}]


# ── Public API ────────────────────────────────────────────────────────────────

def process_document(file_path: str, chunk_size: int = 1000) -> list[dict]:
    """
    Extract text from *file_path* and return chunks in the standard format:
        [{"text": str, "page_start": int, "page_end": int}, ...]

    Raises ValueError for unsupported extensions so the caller can return
    a clean HTTP 400 without an unhandled traceback.
    """
    ext = extension_of(file_path)

    if ext == ".pdf":
        # Delegate entirely to the existing, well-tested PDF pipeline.
        return process_pdf(file_path, chunk_size=chunk_size)

    if ext == ".docx":
        pages = _extract_docx(file_path)
    elif ext in (".txt", ".md", ".markdown"):
        pages = _extract_txt(file_path)
    elif ext == ".pptx":
        pages = _extract_pptx(file_path)
    elif ext in (".xlsx", ".xls"):
        pages = _extract_xlsx(file_path)
    elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"):
        pages = _extract_image(file_path)
    else:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    return _chunks_from_pages(pages, chunk_size=chunk_size)
