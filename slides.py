"""Split an uploaded briefing deck (PDF or PowerPoint) into per-slide images and text.

PDFs are rendered with PyMuPDF. PowerPoint files are first converted to PDF with
LibreOffice (``soffice``) when it is installed; otherwise no slides are produced and
the caller should suggest uploading a PDF instead.

Each slide is returned as ``{"page": 1-based int, "image": jpeg bytes, "mime": "image/jpeg", "text": str}``.
"""

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MAX_SLIDES = int(os.environ.get("MAX_SLIDES", "80"))
SLIDE_TEXT_CHARS = 1500          # per-slide text kept for the model
RENDER_WIDTH = 1400              # px, longest side
JPEG_QUALITY = 82

PDF_MIMES = {"application/pdf"}
PPT_MIMES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
}


class SlideError(Exception):
    pass


def soffice_available() -> bool:
    return bool(shutil.which("soffice") or shutil.which("libreoffice"))


def _pptx_to_pdf(data: bytes, suffix: str) -> bytes:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise SlideError("PowerPoint conversion is not available on this server. Upload the deck as a PDF for slide-by-slide support.")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "deck" + suffix)
        with open(src, "wb") as f:
            f.write(data)
        env = dict(os.environ, HOME=tmp)  # LibreOffice needs a writable profile dir
        try:
            subprocess.run(
                [exe, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmp, src],
                check=True, capture_output=True, timeout=180, env=env,
            )
        except subprocess.TimeoutExpired:
            raise SlideError("Converting the PowerPoint file took too long. Try exporting it as a PDF.")
        except subprocess.CalledProcessError as e:
            logger.warning("soffice failed: %s", (e.stderr or b"")[:500])
            raise SlideError("Could not convert the PowerPoint file. Try exporting it as a PDF.")
        out = os.path.join(tmp, "deck.pdf")
        if not os.path.exists(out):
            raise SlideError("Could not convert the PowerPoint file. Try exporting it as a PDF.")
        with open(out, "rb") as f:
            return f.read()


def _pdf_to_slides(pdf: bytes) -> list:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        raise SlideError("PDF rendering is not installed on this server (PyMuPDF).")
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception:
        raise SlideError("The PDF could not be opened.")
    slides = []
    try:
        n = min(len(doc), MAX_SLIDES)
        for i in range(n):
            page = doc[i]
            rect = page.rect
            longest = max(rect.width, rect.height) or 1
            zoom = min(RENDER_WIDTH / longest, 3.0)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            img = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
            text = " ".join(page.get_text("text").split())
            slides.append({"page": i + 1, "image": img, "mime": "image/jpeg", "text": text[:SLIDE_TEXT_CHARS]})
    finally:
        doc.close()
    return slides


def extract_slides(data: bytes, mime: str, filename: str = "") -> list:
    """Return a list of slide dicts for a PDF or PowerPoint upload; [] for other types."""
    mime = (mime or "").lower()
    lower = (filename or "").lower()
    if mime in PDF_MIMES or lower.endswith(".pdf"):
        return _pdf_to_slides(data)
    if mime in PPT_MIMES or lower.endswith((".pptx", ".ppt")):
        suffix = ".ppt" if lower.endswith(".ppt") or mime == "application/vnd.ms-powerpoint" else ".pptx"
        return _pdf_to_slides(_pptx_to_pdf(data, suffix))
    return []


def deck_context(slides: list, max_chars: int = 14000) -> str:
    """Compact text of the deck for the model's system prompt.

    ``slides`` is an iterable of objects with ``page_index`` and ``text_content`` (or dicts with page/text).
    """
    lines = []
    total = 0
    count = 0
    for s in slides:
        page = getattr(s, "page_index", None) if not isinstance(s, dict) else s.get("page")
        text = (getattr(s, "text_content", None) if not isinstance(s, dict) else s.get("text")) or ""
        count += 1
        text = text.strip() or "(no text on this slide — visual only)"
        budget = max(200, max_chars // max(1, count))
        if len(text) > budget:
            text = text[:budget].rsplit(" ", 1)[0] + "…"
        line = f"Slide {page}: {text}"
        if total + len(line) > max_chars:
            lines.append(f"(… {count} slides in total; remaining slide text omitted for length)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
