"""
PDF Preprocessing
-----------------
Extracts text from a PDF and splits it into overlapping chunks, similar
in spirit to the PIER-QA preprocessing step (minus header/footer DBSCAN
removal and image/table extraction, which are noted as extension points
below to keep this reference implementation simple and dependency-light).
"""

import pymupdf as fitz  # PyMuPDF (new import name; avoids deprecation warning)
import re


def extract_pages_from_pdf(pdf_path: str) -> list[dict]:
    """Extract cleaned text while retaining the original PDF page number."""
    pages = []
    with fitz.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            page_text = page.get_text("text")
            page_text = re.sub(r"\n{3,}", "\n\n", page_text)
            page_text = page_text.strip()
            if page_text:
                pages.append({"text": page_text, "page_number": page_number})
    return pages


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract raw text from every page, preserving the legacy string API."""
    return "\n\n".join(page["text"] for page in extract_pages_from_pdf(pdf_path))


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """
    Split text into character-based chunks with overlap so that context
    isn't lost at chunk boundaries. Mirrors the base paper's 1000-char
    chunking choice for retrieval-time consistency.
    """
    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = end - overlap  # step back for overlap
    return chunks


def process_pdf(pdf_path: str, chunk_size: int = 1000) -> list[dict]:
    """Full preprocessing pipeline with page metadata for source citations."""
    chunks = []
    for page in extract_pages_from_pdf(pdf_path):
        for text in chunk_text(page["text"], chunk_size=chunk_size):
            chunks.append({"text": text, "page_start": page["page_number"], "page_end": page["page_number"]})
    return chunks

    # Extension points (kept out of this simple reference build):
    #  - Header/footer removal (paper uses DBSCAN over element bounding boxes)
    #  - Table extraction -> dict-formatted table chunks (e.g. via pdfplumber)
    #  - Image extraction + captioning -> indexable text (e.g. via a VLM)
