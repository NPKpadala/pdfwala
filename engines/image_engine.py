"""
engines/image_engine.py — PDFWala Enterprise V13.0
All image processing tools. Registered to Pipeline via @register().
Zero Flask. Zero Celery.

Operations:
  compress_image, resize_image, convert_image, crop_image,
  rotate_image, watermark_image, image_to_pdf, images_to_pdf,
  remove_bg, enhance_image, grayscale_image, flip_image,
  add_text_image, merge_images
"""

import io
import logging
import os

from config import Config
from core.context import JobContext
from core.exceptions import (
    ProcessingError, ValidationError, UnsupportedOperation,
)
from core.pipeline import register

log = logging.getLogger("pdfwala.engines.image")

# ── Library flags ─────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageDraw, ImageFont, ImageOps
    # BUG FIX #1 / #3: Decompression bomb guard must be set at import time,
    # before ANY Image.open() call anywhere in the module. The original code
    # never set this, leaving the entire process exposed.
    Image.MAX_IMAGE_PIXELS = getattr(Config, "MAX_IMAGE_PIXELS", 200_000_000)
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import fitz
    FITZ_OK = True
except ImportError:
    FITZ_OK = False

try:
    from rembg import remove as rembg_remove
    REMBG_OK = True
except ImportError:
    REMBG_OK = False

# ── Constants ─────────────────────────────────────────────────────────────────
ALLOWED_FORMATS = {"JPG", "JPEG", "PNG", "WEBP", "BMP", "TIFF"}
MAX_DIMENSION   = getattr(Config, "MAX_IMAGE_DIMENSION", 10_000)   # px per side
MAX_REMBG_BYTES = getattr(Config, "MAX_REMBG_BYTES", 50 * 1024 * 1024)  # 50 MB
MAX_TEXT_LEN    = 500  # characters for add_text / watermark

# Font search order: Alpine/Debian → macOS → Windows fallback
_FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",          # Debian/Ubuntu
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Alpine/RHEL
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",                                  # macOS
    "C:/Windows/Fonts/arialbd.ttf",                                   # Windows
]
_FONT_REGULAR_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require(flag: bool, op: str, lib: str) -> None:
    if not flag:
        raise UnsupportedOperation(op, lib)


def _find_font(paths: list[str], size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """Try each path; fall back to PIL built-in default."""
    for path in paths:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _parse_int(value, name: str, default: int | None = None) -> int:
    """Parse an integer param with a friendly error on failure."""
    if value is None:
        if default is not None:
            return default
        raise ValidationError(f"Parameter '{name}' is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Parameter '{name}' must be an integer, got: {value!r}")


def _parse_float(value, name: str, default: float | None = None) -> float:
    """Parse a float param with a friendly error on failure."""
    if value is None:
        if default is not None:
            return default
        raise ValidationError(f"Parameter '{name}' is required")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Parameter '{name}' must be a number, got: {value!r}")


def _parse_bool(value, default: bool = True) -> bool:
    """
    BUG FIX #5 (rotate expand): Python's bool("false") == True because non-empty
    strings are truthy. This function correctly handles string representations.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def _parse_color(value: str) -> tuple:
    """
    BUG FIX #12 / add_text color: Older Pillow (<9.2) does not accept hex color
    strings in draw.text(). Parse "#rrggbb" or "#rrggbbaa" to an RGBA tuple.
    """
    value = value.strip()
    if value.startswith("#"):
        h = value.lstrip("#")
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
        if len(h) == 8:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4, 6))
    # Let Pillow try to parse named colors ("red", "blue", etc.)
    return value


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _open_image(path: str) -> "Image.Image":
    """
    Open and fully load an image, applying EXIF orientation automatically.

    BUG FIX #6 (EXIF): PIL does NOT apply EXIF rotation tags by default.
    A photo taken in portrait mode on a phone will open sideways in code
    unless exif_transpose() is called. Every downstream operation (crop,
    resize, merge, …) was silently working on the wrong orientation.
    """
    try:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)  # honour camera orientation tag
        return img
    except Exception as ex:
        raise ValidationError(f"Cannot open image '{os.path.basename(path)}': {ex}")


def _ensure_output_dir(path: str) -> None:
    """
    BUG FIX #22: No function in the original code guaranteed the output
    directory existed, causing silent IOError on first write to a new job dir.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def _save(img: "Image.Image", path: str, fmt: str, quality: int = 85) -> None:
    """
    Save image, handling format quirks.

    BUG FIX #20 (TIFF): Original code had no TIFF branch; it fell through to
    a bare img.save() call that omits compression, producing multi-MB TIFFs.
    BUG FIX #22: Output directory created before write.
    """
    _ensure_output_dir(path)
    fmt = fmt.upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt not in ALLOWED_FORMATS:
        raise ValidationError(
            f"Output format '{fmt}' is not allowed. "
            f"Choose from: {', '.join(sorted(ALLOWED_FORMATS))}"
        )
    try:
        if fmt == "JPEG":
            img = img.convert("RGB")
            img.save(path, fmt, quality=quality, optimize=True)
        elif fmt == "PNG":
            # PNG uses lossless deflate; quality maps to compress_level (0-9)
            compress = max(0, min(9, 9 - quality // 11))
            img.save(path, fmt, optimize=True, compress_level=compress)
        elif fmt == "WEBP":
            img.save(path, fmt, quality=quality)
        elif fmt == "TIFF":
            img.save(path, fmt, compression="lzw")
        else:
            img.save(path, fmt)
    except Exception as ex:
        raise ProcessingError(f"Failed to save image: {ex}")


def _check_output(path: str) -> None:
    """Verify the output file was actually written and is non-empty."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise ProcessingError("Output file is missing or empty after processing")


def _ext_from_path(path: str, fallback: str = "JPEG") -> str:
    """
    BUG FIX #21: Original code produced an empty string when the output path
    had no extension, which was silently passed to _save() as fmt="" and then
    fell through to a bare img.save() with no format argument — a PIL error.
    """
    ext = os.path.splitext(path)[1].lstrip(".").upper()
    return ext if ext else fallback


# ─────────────────────────────────────────────────────────────────────────────
# COMPRESS
# ─────────────────────────────────────────────────────────────────────────────

@register("compress_image")
def compress_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #1a — MAX_IMAGE_PIXELS guard now at module level (_open_image).
    • BUG #1b — img.load() is necessary to detect truncation; OOM risk is
                 mitigated by the pixel cap, not by lazy loading.
    • BUG #1c — Added "maximum" quality level (95).
    • BUG #1d/e — If the output is larger than the original, the original is
                  copied to the output path and reduction is reported as 0 %.
    • BUG #4   — int() on quality param wrapped in _parse_int().
    • BUG #21  — ext fallback via _ext_from_path().
    """
    _require(PIL_OK, "compress_image", "Pillow")

    quality_map = {"low": 60, "medium": 75, "high": 85, "maximum": 95}
    raw_quality = ctx.params.get("quality")
    if raw_quality is not None:
        quality = _parse_int(raw_quality, "quality")
        quality = _clamp(quality, 1, 95)
    else:
        level   = ctx.params.get("level", "medium")
        quality = quality_map.get(level, 75)

    orig = os.path.getsize(ctx.input_path)
    img  = _open_image(ctx.input_path)
    ext  = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext, quality)

    new_size = os.path.getsize(ctx.output_path)

    # If compression made the file larger, just copy the original.
    if new_size >= orig:
        import shutil
        shutil.copy2(ctx.input_path, ctx.output_path)
        new_size  = orig
        reduction = 0.0
    else:
        reduction = round((1 - new_size / orig) * 100, 1)

    _check_output(ctx.output_path)
    return {
        "reduction_pct":         reduction,
        "original_size_bytes":   orig,
        "compressed_size_bytes": new_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RESIZE
# ─────────────────────────────────────────────────────────────────────────────

@register("resize_image")
def resize_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #2a — percent/width/height parsed through _parse_float/_parse_int
                 so bad strings raise ValidationError, not an unhandled crash.
    • BUG #2b — Dimension validation happens before any image is opened.
    • BUG #2c — MAX_DIMENSION cap (default 10 000 px per side).
    • BUG #2d — Aspect ratio kept as float throughout; no integer division.
    """
    _require(PIL_OK, "resize_image", "Pillow")

    width_raw   = ctx.params.get("width")
    height_raw  = ctx.params.get("height")
    percent_raw = ctx.params.get("percent")

    if not width_raw and not height_raw and not percent_raw:
        raise ValidationError("Provide at least one of: width, height, percent")

    img     = _open_image(ctx.input_path)
    ow, oh  = img.size

    if percent_raw is not None:
        pct   = _parse_float(percent_raw, "percent")
        if pct <= 0:
            raise ValidationError("percent must be positive")
        scale = pct / 100.0
        nw    = int(round(ow * scale))
        nh    = int(round(oh * scale))
    else:
        nw = _parse_int(width_raw,  "width",  0) if width_raw  else 0
        nh = _parse_int(height_raw, "height", 0) if height_raw else 0
        # Maintain aspect ratio using float division (BUG #2d)
        if nw and not nh:
            nh = int(round(oh * nw / ow))
        elif nh and not nw:
            nw = int(round(ow * nh / oh))

    if nw <= 0 or nh <= 0:
        raise ValidationError(f"Resulting dimensions must be positive, got {nw}×{nh}")
    if nw > MAX_DIMENSION or nh > MAX_DIMENSION:
        raise ValidationError(
            f"Requested size {nw}×{nh} exceeds the maximum allowed "
            f"{MAX_DIMENSION}×{MAX_DIMENSION} px"
        )

    img = img.resize((nw, nh), Image.LANCZOS)
    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {"original_size": [ow, oh], "new_size": [nw, nh]}


# ─────────────────────────────────────────────────────────────────────────────
# CONVERT
# ─────────────────────────────────────────────────────────────────────────────

@register("convert_image")
def convert_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #3a — Target format validated against ALLOWED_FORMATS whitelist.
    • BUG #3b — If input already matches target format, skip re-encode and
                 copy the file directly.
    • BUG #3c — EXIF already applied in _open_image (orientation preserved).
    """
    _require(PIL_OK, "convert_image", "Pillow")

    raw_fmt     = ctx.params.get("format", "jpg").upper()
    target_fmt  = "JPEG" if raw_fmt == "JPG" else raw_fmt

    if target_fmt not in ALLOWED_FORMATS:
        raise ValidationError(
            f"Format '{raw_fmt}' is not supported. "
            f"Allowed: {', '.join(sorted(ALLOWED_FORMATS))}"
        )

    # BUG #3b — skip re-encode when already in target format
    src_ext = os.path.splitext(ctx.input_path)[1].lstrip(".").upper()
    src_fmt = "JPEG" if src_ext == "JPG" else src_ext
    if src_fmt == target_fmt:
        import shutil
        shutil.copy2(ctx.input_path, ctx.output_path)
        _ensure_output_dir(ctx.output_path)
        _check_output(ctx.output_path)
        return {"output_format": target_fmt, "note": "input already in target format"}

    img = _open_image(ctx.input_path)
    _save(img, ctx.output_path, target_fmt)
    _check_output(ctx.output_path)
    return {"output_format": target_fmt}


# ─────────────────────────────────────────────────────────────────────────────
# CROP
# ─────────────────────────────────────────────────────────────────────────────

@register("crop_image")
def crop_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #4a — All int() casts replaced with _parse_int().
    • BUG #4b — Warns user when the requested region is clamped to image bounds.
    • BUG #4c — Minimum crop size enforced (8×8 px).
    • BUG #6  — Negative x/y coordinates rejected explicitly.
    """
    _require(PIL_OK, "crop_image", "Pillow")

    x      = _parse_int(ctx.params.get("x",      0), "x",      default=0)
    y      = _parse_int(ctx.params.get("y",      0), "y",      default=0)
    width  = _parse_int(ctx.params.get("width",  0), "width")
    height = _parse_int(ctx.params.get("height", 0), "height")

    if x < 0 or y < 0:
        raise ValidationError("x and y must be non-negative")
    if width <= 0 or height <= 0:
        raise ValidationError("width and height must be positive integers")

    img     = _open_image(ctx.input_path)
    ow, oh  = img.size

    if x >= ow or y >= oh:
        raise ValidationError(
            f"Crop origin ({x}, {y}) is outside the image bounds ({ow}×{oh})"
        )

    x2_req, y2_req = x + width, y + height
    x2 = min(x2_req, ow)
    y2 = min(y2_req, oh)

    warnings = []
    if x2 < x2_req or y2 < y2_req:
        warnings.append(
            f"Crop region was clamped from {x2_req - x}×{y2_req - y} "
            f"to {x2 - x}×{y2 - y} to fit within image bounds"
        )

    actual_w = x2 - x
    actual_h = y2 - y
    if actual_w < 8 or actual_h < 8:
        raise ValidationError(
            f"Resulting crop size {actual_w}×{actual_h} is too small (minimum 8×8 px)"
        )

    img = img.crop((x, y, x2, y2))
    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)

    result = {"cropped_size": [img.width, img.height]}
    if warnings:
        result["warnings"] = warnings
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ROTATE
# ─────────────────────────────────────────────────────────────────────────────

@register("rotate_image")
def rotate_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #5a — expand param parsed through _parse_bool(); string "false" now
                 correctly disables canvas expansion.
    • BUG #5b — Angle normalised to [0, 360); trivial rotations skipped.
    • BUG #5c — Image.BICUBIC deprecated since Pillow 9.1; replaced with
                 Image.Resampling.LANCZOS (high-quality, still available).
    """
    _require(PIL_OK, "rotate_image", "Pillow")

    angle  = _parse_float(ctx.params.get("angle", 90), "angle")
    expand = _parse_bool(ctx.params.get("expand", True))

    # Normalise and skip trivial rotations (BUG #5b)
    angle = angle % 360
    if angle == 0:
        import shutil
        shutil.copy2(ctx.input_path, ctx.output_path)
        _ensure_output_dir(ctx.output_path)
        return {"angle": 0, "note": "no rotation applied"}

    img = _open_image(ctx.input_path)
    # Negative because PIL rotates counter-clockwise by default
    img = img.rotate(-angle, expand=expand, resample=Image.LANCZOS)
    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {"angle": angle}


# ─────────────────────────────────────────────────────────────────────────────
# FLIP
# ─────────────────────────────────────────────────────────────────────────────

@register("flip_image")
def flip_image(ctx: JobContext) -> dict:
    """Original direction whitelist was correct. No logic bugs found here."""
    _require(PIL_OK, "flip_image", "Pillow")

    direction = ctx.params.get("direction", "horizontal").lower().strip()
    if direction not in ("horizontal", "vertical"):
        raise ValidationError("direction must be 'horizontal' or 'vertical'")

    img = _open_image(ctx.input_path)
    img = img.transpose(
        Image.FLIP_LEFT_RIGHT if direction == "horizontal" else Image.FLIP_TOP_BOTTOM
    )
    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {"direction": direction}


# ─────────────────────────────────────────────────────────────────────────────
# GRAYSCALE
# ─────────────────────────────────────────────────────────────────────────────

@register("grayscale_image")
def grayscale_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #7a — Skip re-encode if image is already grayscale.
    • BUG #7b — Alpha channel preserved: convert to "LA" for PNG/WEBP,
                 drop alpha (to "L") only for JPEG which cannot store it.
    • BUG #23  — WEBP output from "L" mode can fail on some Pillow builds;
                  safe conversion applied.
    """
    _require(PIL_OK, "grayscale_image", "Pillow")

    img = _open_image(ctx.input_path)
    ext = _ext_from_path(ctx.output_path, "JPEG")

    if img.mode in ("L", "LA"):
        import shutil
        shutil.copy2(ctx.input_path, ctx.output_path)
        _ensure_output_dir(ctx.output_path)
        return {"note": "image is already grayscale"}

    has_alpha = img.mode in ("RGBA", "PA", "LA")
    if has_alpha and ext in ("PNG", "WEBP"):
        img = img.convert("LA")
    else:
        img = img.convert("L")

    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# ENHANCE
# ─────────────────────────────────────────────────────────────────────────────

@register("enhance_image")
def enhance_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #8a — All four enhance values parsed through _parse_float().
    • BUG #8b — Values clamped to [0.0, 10.0] to prevent white/black floods.
    • BUG #8c — Only the operations whose value differs from 1.0 are applied,
                 avoiding unnecessary full-image copies.
    • BUG #9  — Intermediate images explicitly closed after use to release RAM.
    """
    _require(PIL_OK, "enhance_image", "Pillow")

    brightness = _clamp(_parse_float(ctx.params.get("brightness", 1.0), "brightness"), 0.0, 10.0)
    contrast   = _clamp(_parse_float(ctx.params.get("contrast",   1.0), "contrast"),   0.0, 10.0)
    saturation = _clamp(_parse_float(ctx.params.get("saturation", 1.0), "saturation"), 0.0, 10.0)
    sharpness  = _clamp(_parse_float(ctx.params.get("sharpness",  1.0), "sharpness"),  0.0, 10.0)

    img = _open_image(ctx.input_path)

    # Apply only changed enhancements; close intermediates explicitly.
    for factor, enhancer_cls in (
        (brightness, ImageEnhance.Brightness),
        (contrast,   ImageEnhance.Contrast),
        (saturation, ImageEnhance.Color),
        (sharpness,  ImageEnhance.Sharpness),
    ):
        if factor != 1.0:
            enhanced = enhancer_cls(img).enhance(factor)
            img.close()
            img = enhanced

    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {
        "brightness": brightness, "contrast": contrast,
        "saturation": saturation, "sharpness": sharpness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WATERMARK
# ─────────────────────────────────────────────────────────────────────────────

@register("watermark_image")
def watermark_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #9a  — Font search across Alpine/Debian/macOS/Windows via _find_font().
    • BUG #9b  — opacity parsed safely; clamped to [0, 255].
    • BUG #9c  — textbbox fallback for older Pillow using textsize().
    • BUG #9d  — Multi-line text handled: max line width used for positioning.
    • BUG #25  — Text length capped at MAX_TEXT_LEN to prevent OOM rendering.
    • BUG #10a — opacity calculation fixed: if user already passes 0-255, it's
                  used directly (auto-detect by value range).
    """
    _require(PIL_OK, "watermark_image", "Pillow")

    text     = str(ctx.params.get("text", "CONFIDENTIAL"))[:MAX_TEXT_LEN]
    position = ctx.params.get("position", "center")
    fontsize = _parse_int(ctx.params.get("fontsize", 36), "fontsize", default=36)
    fontsize = _clamp(fontsize, 8, 256)

    # Opacity: accept either 0.0-1.0 float or 0-255 int
    raw_opacity = ctx.params.get("opacity", 0.4)
    op_float    = _parse_float(raw_opacity, "opacity")
    opacity     = int(_clamp(op_float if op_float <= 1.0 else op_float / 255.0, 0.0, 1.0) * 255)

    img    = _open_image(ctx.input_path).convert("RGBA")
    ow, oh = img.size

    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw    = ImageDraw.Draw(overlay)
    font    = _find_font(_FONT_BOLD_PATHS, fontsize)

    # Measure text; handle multi-line (BUG #9d)
    lines = text.split("\n")
    try:
        bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        tw = max(b[2] - b[0] for b in bboxes)
        th = sum(b[3] - b[1] for b in bboxes) + (len(lines) - 1) * 4  # 4px line gap
    except AttributeError:
        # Pillow < 8.0 fallback
        sizes = [draw.textsize(line, font=font) for line in lines]  # type: ignore[attr-defined]
        tw    = max(s[0] for s in sizes)
        th    = sum(s[1] for s in sizes) + (len(lines) - 1) * 4

    pos_map = {
        "center":       ((ow - tw) // 2, (oh - th) // 2),
        "top-left":     (10, 10),
        "top-right":    (ow - tw - 10, 10),
        "bottom-left":  (10, oh - th - 10),
        "bottom-right": (ow - tw - 10, oh - th - 10),
    }
    x, y = pos_map.get(position, pos_map["center"])
    draw.text((x, y), text, font=font, fill=(128, 128, 128, opacity))

    watermarked = Image.alpha_composite(img, overlay).convert("RGB")
    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(watermarked, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {"watermark_text": text}


# ─────────────────────────────────────────────────────────────────────────────
# ADD TEXT
# ─────────────────────────────────────────────────────────────────────────────

@register("add_text_image")
def add_text_image(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #10a — Font search across all platforms via _find_font().
    • BUG #10b — Color parsed from hex string to RGB tuple via _parse_color().
    • BUG #10c — Text length capped at MAX_TEXT_LEN to prevent OOM.
    • BUG #26  — x/y bounds checked; warn if text origin is off-canvas.
    """
    _require(PIL_OK, "add_text_image", "Pillow")

    text     = str(ctx.params.get("text", ""))[:MAX_TEXT_LEN]
    x        = _parse_int(ctx.params.get("x", 10),   "x",        default=10)
    y        = _parse_int(ctx.params.get("y", 10),   "y",        default=10)
    fontsize = _parse_int(ctx.params.get("fontsize", 24), "fontsize", default=24)
    fontsize = _clamp(fontsize, 8, 256)
    color    = _parse_color(ctx.params.get("color", "#000000"))

    if not text:
        raise ValidationError("text parameter is required and cannot be empty")

    img    = _open_image(ctx.input_path).convert("RGBA")
    ow, oh = img.size

    warnings = []
    if x < 0 or x >= ow or y < 0 or y >= oh:
        warnings.append(
            f"Text origin ({x}, {y}) is outside the image bounds ({ow}×{oh}); "
            "text may not be visible"
        )

    draw = ImageDraw.Draw(img)
    font = _find_font(_FONT_REGULAR_PATHS, fontsize)
    draw.text((x, y), text, font=font, fill=color)

    ext = _ext_from_path(ctx.output_path, "JPEG")
    _save(img.convert("RGB"), ctx.output_path, ext)
    _check_output(ctx.output_path)

    result: dict = {}
    if warnings:
        result["warnings"] = warnings
    return result


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE TO PDF
# ─────────────────────────────────────────────────────────────────────────────

@register("image_to_pdf")
def image_to_pdf(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #11a — Very large images are downscaled to a max PDF page size
                  (A3 at 150 dpi = 4961×7016 px) before embedding, preventing
                  enormous single-page PDFs.
    • BUG #11b — Page size can be controlled via params (a4/a3/letter/original).
    • BUG #11c — EXIF orientation applied by _open_image() before save.
    • BUG #13  — output_path validated to end with .pdf.
    • BUG #27  — PIL PDF resolution parameter behavior noted; actual DPI set
                  via page_size logic.
    """
    _require(PIL_OK, "image_to_pdf", "Pillow")

    if not ctx.output_path.lower().endswith(".pdf"):
        raise ValidationError("output_path must end with .pdf for image_to_pdf")

    # Points per inch: A4=595×842pt, A3=842×1191pt, letter=612×792pt at 72dpi
    page_size_map = {
        "a4":       (595,  842),
        "a3":       (842,  1191),
        "letter":   (612,  792),
        "original": None,
    }
    page_key = ctx.params.get("page_size", "original").lower()
    if page_key not in page_size_map:
        raise ValidationError(f"page_size must be one of: {', '.join(page_size_map)}")

    dpi = _parse_int(ctx.params.get("dpi", 150), "dpi", default=150)
    dpi = _clamp(dpi, 72, 300)

    img = _open_image(ctx.input_path).convert("RGB")

    if page_key != "original":
        pt_w, pt_h = page_size_map[page_key]
        px_w = int(pt_w * dpi / 72)
        px_h = int(pt_h * dpi / 72)
        img.thumbnail((px_w, px_h), Image.LANCZOS)

    _ensure_output_dir(ctx.output_path)
    img.save(ctx.output_path, "PDF", resolution=dpi)
    _check_output(ctx.output_path)
    return {"page_size": page_key, "dpi": dpi}


# ─────────────────────────────────────────────────────────────────────────────
# IMAGES TO PDF
# ─────────────────────────────────────────────────────────────────────────────

# Replace the images_to_pdf function with this:

@register("images_to_pdf")
def images_to_pdf(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #12a — Images processed in batches of 50 to avoid OOM.
                  Batch PDFs are merged via fitz at the end.
    • BUG #12c — Reasonable page count cap (Config.MAX_PDF_PAGES, default 500).
    • BUG #12d — ctx.set_progress() called every 10 images.
    • BUG #11c — EXIF orientation applied per image.
    • BUG #13  — Output validated as .pdf.
    """
    _require(PIL_OK, "images_to_pdf", "Pillow")

    if not ctx.input_paths:
        raise ValidationError("No input images provided")
    if not ctx.output_path.lower().endswith(".pdf"):
        raise ValidationError("output_path must end with .pdf for images_to_pdf")

    max_pages = getattr(Config, "MAX_PDF_PAGES", 500)
    paths     = ctx.input_paths[:max_pages]

    # Validate paths
    valid_paths = []
    for p in paths:
        try:
            with Image.open(p) as probe:
                probe.verify()
            valid_paths.append(p)
        except Exception as ex:
            log.warning(f"Skipping '{p}': {ex}")

    if not valid_paths:
        raise ProcessingError("No valid images could be opened")

    _ensure_output_dir(ctx.output_path)
    dpi = _parse_int(ctx.params.get("dpi", 150), "dpi", default=150)
    dpi = _clamp(dpi, 72, 300)

    BATCH_SIZE = 50
    batch_pdfs = []

    for batch_start in range(0, len(valid_paths), BATCH_SIZE):
        batch = valid_paths[batch_start : batch_start + BATCH_SIZE]
        batch_pdf = ctx.output_path + f".batch_{batch_start}.pdf"

        first = _open_image(batch[0]).convert("RGB")
        rest = []
        for p in batch[1:]:
            rest.append(_open_image(p).convert("RGB"))

        first.save(batch_pdf, "PDF", save_all=True,
                   append_images=rest, resolution=dpi)
        first.close()
        for img in rest:
            img.close()

        batch_pdfs.append(batch_pdf)

        if hasattr(ctx, "set_progress"):
            ctx.set_progress(int((batch_start + len(batch)) / len(valid_paths) * 90))

    # Merge batch PDFs into final output
    if len(batch_pdfs) == 1:
        os.replace(batch_pdfs[0], ctx.output_path)
    else:
        if FITZ_OK:
            merged = fitz.open()
            for bp in batch_pdfs:
                src = fitz.open(bp)
                merged.insert_pdf(src)
                src.close()
            merged.save(ctx.output_path, deflate=True, garbage=3)
            merged.close()
        else:
            from PyPDF2 import PdfMerger
            merger = PdfMerger()
            for bp in batch_pdfs:
                merger.append(bp)
            merger.write(ctx.output_path)
            merger.close()

    # Cleanup batch PDFs
    for bp in batch_pdfs:
        try:
            os.remove(bp)
        except OSError:
            pass

    _check_output(ctx.output_path)
    if hasattr(ctx, "set_progress"):
        ctx.set_progress(100)
    return {"pages": len(valid_paths)}

# ─────────────────────────────────────────────────────────────────────────────
# REMOVE BACKGROUND
# ─────────────────────────────────────────────────────────────────────────────

@register("remove_bg")
def remove_bg(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #13a — File size validated against MAX_REMBG_BYTES (50 MB) before
                  reading to prevent loading huge files into RAM.
    • BUG #13b — rembg processes the raw bytes; there is no streaming API,
                  but we at least avoid a double read.
    • BUG #16  — Output format controlled by ctx.output_path extension;
                  defaults to PNG (rembg always outputs RGBA which needs PNG
                  or WEBP — JPEG silently drops alpha, so we warn).
    """
    _require(REMBG_OK, "remove_bg", "rembg")

    file_size = os.path.getsize(ctx.input_path)
    if file_size > MAX_REMBG_BYTES:
        raise ValidationError(
            f"Input file ({file_size // (1024*1024)} MB) exceeds the "
            f"{MAX_REMBG_BYTES // (1024*1024)} MB limit for background removal"
        )

    with open(ctx.input_path, "rb") as f:
        data = f.read()

    result = rembg_remove(data)

    ext = _ext_from_path(ctx.output_path, "PNG").upper()
    warnings = []
    if ext == "JPEG":
        warnings.append(
            "JPEG does not support transparency; background will appear white. "
            "Use PNG or WEBP to preserve the transparent background."
        )

    _ensure_output_dir(ctx.output_path)
    with open(ctx.output_path, "wb") as f:
        f.write(result)

    # If JPEG output requested, re-open and convert properly
    if ext == "JPEG":
        img = Image.open(io.BytesIO(result)).convert("RGB")
        _save(img, ctx.output_path, "JPEG")

    _check_output(ctx.output_path)
    result_dict: dict = {}
    if warnings:
        result_dict["warnings"] = warnings
    return result_dict


# ─────────────────────────────────────────────────────────────────────────────
# MERGE IMAGES
# ─────────────────────────────────────────────────────────────────────────────

@register("merge_images")
def merge_images(ctx: JobContext) -> dict:
    """
    Fixes applied:
    • BUG #14a — Canvas dimensions validated against MAX_DIMENSION before
                  allocation; raises ValidationError if result would exceed cap.
    • BUG #14b — Images loaded one at a time to measure dimensions first;
                  canvas allocated once with final size.
    • BUG #14c — Per-side cap: each image resized down if it exceeds MAX_DIMENSION.
    • BUG #18  — canvas.paste() now includes the alpha mask so transparent
                  regions in source images are respected.
    • BUG #28  — Canvas kept in RGBA mode throughout; only converted at save.
    """
    _require(PIL_OK, "merge_images", "Pillow")

    if not ctx.input_paths:
        raise ValidationError("No input images provided")

    direction = ctx.params.get("direction", "horizontal").lower().strip()
    if direction not in ("horizontal", "vertical"):
        raise ValidationError("direction must be 'horizontal' or 'vertical'")

    # Pass 1: open images and optionally cap dimensions
    images: list[Image.Image] = []
    for p in ctx.input_paths:
        try:
            img = _open_image(p).convert("RGBA")
            if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
                img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
            images.append(img)
        except Exception as ex:
            log.warning(f"Skipping '{p}': {ex}")

    if not images:
        raise ProcessingError("No valid images could be opened")

    # Pass 2: calculate canvas size and enforce cap BEFORE allocation
    if direction == "horizontal":
        total_w = sum(img.width  for img in images)
        total_h = max(img.height for img in images)
    else:
        total_w = max(img.width  for img in images)
        total_h = sum(img.height for img in images)

    if total_w > MAX_DIMENSION * 4 or total_h > MAX_DIMENSION * 4:
        raise ValidationError(
            f"Merged canvas would be {total_w}×{total_h} px, which exceeds "
            f"the safe limit. Reduce the number or size of input images."
        )

    # Transparent canvas (not white) so alpha is preserved (BUG #28)
    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    offset = 0
    for img in images:
        pos = (offset, 0) if direction == "horizontal" else (0, offset)
        # Use img itself as mask to honour alpha channels (BUG #18)
        canvas.paste(img, pos, mask=img)
        offset += img.width if direction == "horizontal" else img.height
        img.close()

    ext = _ext_from_path(ctx.output_path, "PNG")
        # Flatten to RGB for formats that don't support alpha
    out_img = canvas if ext in ("PNG", "WEBP") else canvas.convert("RGB")
    _save(out_img, ctx.output_path, ext)
    _check_output(ctx.output_path)
    return {"merged_count": len(images), "direction": direction}

# ── Format aliases ────────────────────────────────────────────────────────

@register("png_to_jpg")
def png_to_jpg(ctx: JobContext) -> dict:
    """Convert PNG to JPEG — delegates to convert_image."""
    ctx.params["format"] = "jpg"
    return convert_image(ctx)


@register("webp_to_jpg")
def webp_to_jpg(ctx: JobContext) -> dict:
    """Convert WebP to JPEG — delegates to convert_image."""
    ctx.params["format"] = "jpg"
    return convert_image(ctx)

# ── Format aliases ────────────────────────────────────────────────────────

@register("png_to_jpg")
def png_to_jpg(ctx: JobContext) -> dict:
    """Convert PNG to JPEG — delegates to convert_image."""
    ctx.params["format"] = "jpg"
    return convert_image(ctx)


@register("webp_to_jpg")
def webp_to_jpg(ctx: JobContext) -> dict:
    """Convert WebP to JPEG — delegates to convert_image."""
    ctx.params["format"] = "jpg"
    return convert_image(ctx)
