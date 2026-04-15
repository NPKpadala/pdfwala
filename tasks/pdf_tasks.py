"""
PDFWala V10.0
tasks/pdf_tasks.py — Celery tasks for PDF operations: compress, merge, split, watermark.
"""

import io
import os
import shutil
import zipfile
import logging
from datetime import datetime

from workers.celery_app import celery_app
from services.redis_service import redis_service
from services.queue_service import cb_ghostscript
from utils.helpers import get_timestamp

log = logging.getLogger("pdfwala.tasks.pdf")

# ── Ghostscript helper (module-local) ─────────────────────────────────────────
import subprocess
from config import Config


def _ghostscript_compress(
    input_path: str,
    output_path: str,
    gs_setting: str = "/ebook",
    extra_flags: list = None,
    timeout: int = 300,
) -> bool:
    if not cb_ghostscript.can_execute():
        log.error("CircuitBreaker[ghostscript] OPEN")
        return False
    cmd = [
        Config.GHOSTSCRIPT,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dNOSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={output_path}",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(input_path)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            cb_ghostscript.record_failure()
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            cb_ghostscript.record_failure()
            return False
        cb_ghostscript.record_success()
        return True
    except subprocess.TimeoutExpired:
        cb_ghostscript.record_failure()
        return False
    except Exception as ex:
        cb_ghostscript.record_failure()
        log.error(f"Ghostscript exception: {ex}")
        return False


# ── Tasks ──────────────────────────────────────────────────────────────────────

if celery_app is not None:

    @celery_app.task(
        bind=True,
        max_retries=3,
        name="pdfwala.tasks.pdf_tasks.compress_pdf_task",
        queue="fast",
    )
    def compress_pdf_task(
        self,
        input_path: str,
        output_path: str,
        job_id: str,
        quality: str = "medium",
    ):
        """Async PDF compression with Ghostscript + PyMuPDF two-stage pipeline."""
        try:
            import fitz
            from PIL import Image

            redis_service.job_update(job_id, {"status": "processing"})
            cfg = {
                "low":    {"dpi": 150, "quality": 85, "gs": "/printer"},
                "medium": {"dpi": 120, "quality": 72, "gs": "/printer"},
                "high":   {"dpi": 96,  "quality": 60, "gs": "/ebook"},
            }.get(quality, {"dpi": 120, "quality": 72, "gs": "/printer"})

            orig = os.path.getsize(input_path)

            # Stage 1: PyMuPDF image downsampling
            stage1 = output_path + "_s1.pdf"
            try:
                doc      = fitz.open(input_path)
                modified = False
                for page in doc:
                    for img in page.get_images(full=True):
                        xref = img[0]
                        try:
                            base = doc.extract_image(xref)
                            if not base:
                                continue
                            pil = Image.open(io.BytesIO(base["image"]))
                            ow, oh   = pil.size
                            src_dpi  = max(base.get("xres", 150), base.get("yres", 150), 1)
                            scale    = min(1.0, cfg["dpi"] / src_dpi)
                            if scale >= 0.95:
                                continue
                            nw = max(1, int(ow * scale))
                            nh = max(1, int(oh * scale))
                            pil = pil.resize((nw, nh), Image.LANCZOS)
                            if pil.mode in ("RGBA", "P", "LA"):
                                bg = Image.new("RGB", pil.size, (255, 255, 255))
                                if pil.mode == "P":
                                    pil = pil.convert("RGBA")
                                mask = pil.split()[-1] if pil.mode in ("RGBA", "LA") else None
                                bg.paste(pil, mask=mask)
                                pil = bg
                            elif pil.mode != "RGB":
                                pil = pil.convert("RGB")
                            buf_img = io.BytesIO()
                            pil.save(buf_img, "JPEG", quality=cfg["quality"],
                                     optimize=True, progressive=True)
                            doc.update_stream(xref, buf_img.getvalue())
                            modified = True
                        except Exception:
                            pass
                if modified:
                    doc.save(stage1, deflate=True, deflate_images=True,
                             deflate_fonts=True, garbage=3, clean=False)
                else:
                    shutil.copy(input_path, stage1)
                doc.close()
            except Exception as ex:
                log.warning(f"compress stage1 error: {ex}")
                shutil.copy(input_path, stage1)

            stage1_size = os.path.getsize(stage1)

            # Stage 2: Ghostscript
            gs_out  = output_path + "_gs.pdf"
            gs_ok   = _ghostscript_compress(
                stage1, gs_out, cfg["gs"],
                extra_flags=[
                    "-dColorImageDownsampleType=/Bicubic",
                    f"-dColorImageResolution={cfg['dpi']}",
                    f"-dGrayImageResolution={cfg['dpi']}",
                ],
            )
            chosen = None
            if gs_ok and os.path.exists(gs_out) and os.path.getsize(gs_out) < stage1_size:
                chosen = gs_out
            if chosen is None and os.path.exists(stage1) and stage1_size < orig:
                chosen = stage1
            if chosen is None:
                chosen = input_path
            shutil.copy(chosen, output_path)

            # Cleanup temp files
            for tmp in [stage1, gs_out]:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

            new_size  = os.path.getsize(output_path)
            reduction = round((1 - new_size / orig) * 100, 1) if orig else 0

            redis_service.job_update(job_id, {
                "status":        "completed",
                "progress":      "100",
                "output_path":   output_path,
                "reduction_pct": str(reduction),
                "completed_at":  get_timestamp(),
            })
            return {"status": "completed", "output": output_path}

        except Exception as ex:
            log.error(f"compress_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            delay = 30 * (3 ** self.request.retries)
            raise self.retry(exc=ex, countdown=delay, max_retries=3)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.pdf_tasks.merge_pdf_task",
        queue="fast",
    )
    def merge_pdf_task(self, input_paths: list, output_path: str, job_id: str):
        """Async PDF merge task."""
        try:
            from PyPDF2 import PdfMerger
            redis_service.job_update(job_id, {"status": "processing"})
            merger = PdfMerger()
            for p in input_paths:
                merger.append(p)
            merger.write(output_path)
            merger.close()
            redis_service.job_update(job_id, {
                "status":      "completed",
                "progress":    "100",
                "output_path": output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}
        except Exception as ex:
            log.error(f"merge_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            for p in input_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.pdf_tasks.split_pdf_task",
        queue="fast",
    )
    def split_pdf_task(self, input_path: str, output_path: str, job_id: str,
                       page_indices: list = None):
        """Async PDF split to ZIP task."""
        try:
            from PyPDF2 import PdfReader, PdfWriter
            redis_service.job_update(job_id, {"status": "processing"})
            reader  = PdfReader(input_path)
            total   = len(reader.pages)
            indices = page_indices if page_indices is not None else list(range(total))
            buf     = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx in indices:
                    w  = PdfWriter()
                    w.add_page(reader.pages[idx])
                    pb = io.BytesIO()
                    w.write(pb)
                    zf.writestr(f"page_{idx+1:04d}.pdf", pb.getvalue())
            with open(output_path, "wb") as fh:
                fh.write(buf.getvalue())
            redis_service.job_update(job_id, {
                "status":      "completed",
                "progress":    "100",
                "output_path": output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}
        except Exception as ex:
            log.error(f"split_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.pdf_tasks.watermark_pdf_task",
        queue="fast",
    )
    def watermark_pdf_task(
        self, input_path: str, output_path: str, job_id: str,
        text: str = "CONFIDENTIAL", opacity: float = 0.3,
        color: str = "808080", position: str = "diagonal", rotation: float = 45.0,
    ):
        """Async watermark task."""
        try:
            import fitz
            from utils.pdf_utils import create_watermark_pdf
            redis_service.job_update(job_id, {"status": "processing"})
            doc = fitz.open(input_path)
            try:
                for page in doc:
                    r  = page.rect
                    wm = create_watermark_pdf(
                        text, opacity, color, r.width, r.height, position, rotation
                    )
                    wmpdf = fitz.open("pdf", wm)
                    page.show_pdf_page(
                        fitz.Rect(0, 0, r.width, r.height), wmpdf, 0, overlay=True
                    )
                    wmpdf.close()
                doc.save(output_path)
            finally:
                doc.close()
            redis_service.job_update(job_id, {
                "status":      "completed",
                "progress":    "100",
                "output_path": output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}
        except Exception as ex:
            log.error(f"watermark_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

else:
    # Stubs when Celery unavailable
    compress_pdf_task  = None
    merge_pdf_task     = None
    split_pdf_task     = None
    watermark_pdf_task = None
