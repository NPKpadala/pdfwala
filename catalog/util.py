"""Shared pure helpers for the catalog (no data, no I/O)."""

# Domain acronyms so slug fallbacks read correctly (pdf-to-jpg -> PDF to JPG).
_ACRONYMS = {
    "pdf": "PDF", "jpg": "JPG", "png": "PNG", "html": "HTML", "csv": "CSV",
    "json": "JSON", "ocr": "OCR", "ppt": "PPT", "pdfa": "PDF/A", "docx": "DOCX",
    "xlsx": "XLSX", "pptx": "PPTX", "ai": "AI", "id": "ID", "url": "URL",
}
_SMALL = {"to", "and", "of", "for", "a", "the", "by", "with", "in", "on"}


def pretty_slug(slug: str) -> str:
    """Human label for a slug when no display_name is set. 'pdf-to-jpg' -> 'PDF to JPG'."""
    parts = (slug or "").split("-")
    out = []
    for i, w in enumerate(parts):
        if w in _ACRONYMS:
            out.append(_ACRONYMS[w])
        elif i > 0 and w in _SMALL:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)
