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
    from PIL import Image, ImageEnhance, ImageFilter, ImageDraw, ImageFont
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


def _require(flag: bool, op: str, lib: str):
    if not flag:
        raise UnsupportedOperation(op, lib)


def _open_image(path: str) -> "Image.Image":
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception as ex:
        raise ValidationError(f"Cannot open image: {ex}")


def _save(img: "Image.Image", path: str, fmt: str, quality: int = 85) -> None:
    fmt = fmt.upper()
    if fmt == "JPG":
        fmt = "JPEG"
    try:
        if fmt == "JPEG":
            img = img.convert("RGB")
            img.save(path, fmt, quality=quality, optimize=True)
        elif fmt == "PNG":
            img.save(path, fmt, optimize=True)
        elif fmt == "WEBP":
            img.save(path, fmt, quality=quality)
        else:
            img.save(path, fmt)
    except Exception as ex:
        raise ProcessingError(f"Failed to save image: {ex}")


# ─────────────────────────────────────────────────────────────────────────────
# COMPRESS
# ─────────────────────────────────────────────────────────────────────────────

@register("compress_image")
def compress_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "compress_image", "Pillow")
    quality_map = {"low": 60, "medium": 75, "high": 85}
    quality = int(ctx.params.get("quality",
                  quality_map.get(ctx.params.get("level", "medium"), 75)))
    orig    = os.path.getsize(ctx.input_path)
    img     = _open_image(ctx.input_path)
    ext     = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext, quality)
    new_size   = os.path.getsize(ctx.output_path)
    reduction  = round((1 - new_size / orig) * 100, 1) if orig else 0
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
    _require(PIL_OK, "resize_image", "Pillow")
    width   = ctx.params.get("width")
    height  = ctx.params.get("height")
    percent = ctx.params.get("percent")
    if not width and not height and not percent:
        raise ValidationError("Provide width, height, or percent")

    img = _open_image(ctx.input_path)
    ow, oh = img.size

    if percent:
        scale  = float(percent) / 100
        nw, nh = int(ow * scale), int(oh * scale)
    else:
        nw = int(width)  if width  else 0
        nh = int(height) if height else 0
        if nw and not nh:
            nh = int(oh * nw / ow)
        elif nh and not nw:
            nw = int(ow * nh / oh)

    if nw <= 0 or nh <= 0:
        raise ValidationError("Resulting dimensions must be positive")

    img = img.resize((nw, nh), Image.LANCZOS)
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {"original_size": [ow, oh], "new_size": [nw, nh]}


# ─────────────────────────────────────────────────────────────────────────────
# CONVERT
# ─────────────────────────────────────────────────────────────────────────────

@register("convert_image")
def convert_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "convert_image", "Pillow")
    target_fmt = ctx.params.get("format", "jpg").upper()
    if target_fmt == "JPG":
        target_fmt = "JPEG"

    img = _open_image(ctx.input_path)
    _save(img, ctx.output_path, target_fmt)
    return {"output_format": target_fmt}


# ─────────────────────────────────────────────────────────────────────────────
# CROP
# ─────────────────────────────────────────────────────────────────────────────

@register("crop_image")
def crop_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "crop_image", "Pillow")
    x      = int(ctx.params.get("x", 0))
    y      = int(ctx.params.get("y", 0))
    width  = int(ctx.params.get("width",  0))
    height = int(ctx.params.get("height", 0))
    if width <= 0 or height <= 0:
        raise ValidationError("width and height must be positive integers")

    img = _open_image(ctx.input_path)
    ow, oh = img.size
    x2 = min(x + width,  ow)
    y2 = min(y + height, oh)
    if x >= ow or y >= oh or x2 <= x or y2 <= y:
        raise ValidationError("Crop region is outside image bounds")

    img = img.crop((x, y, x2, y2))
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {"cropped_size": [img.width, img.height]}


# ─────────────────────────────────────────────────────────────────────────────
# ROTATE
# ─────────────────────────────────────────────────────────────────────────────

@register("rotate_image")
def rotate_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "rotate_image", "Pillow")
    angle  = float(ctx.params.get("angle", 90))
    expand = ctx.params.get("expand", True)

    img = _open_image(ctx.input_path)
    img = img.rotate(-angle, expand=bool(expand), resample=Image.BICUBIC)
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {"angle": angle}


# ─────────────────────────────────────────────────────────────────────────────
# FLIP
# ─────────────────────────────────────────────────────────────────────────────

@register("flip_image")
def flip_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "flip_image", "Pillow")
    direction = ctx.params.get("direction", "horizontal").lower()
    if direction not in ("horizontal", "vertical"):
        raise ValidationError("direction must be 'horizontal' or 'vertical'")

    img = _open_image(ctx.input_path)
    img = img.transpose(
        Image.FLIP_LEFT_RIGHT if direction == "horizontal" else Image.FLIP_TOP_BOTTOM
    )
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {"direction": direction}


# ─────────────────────────────────────────────────────────────────────────────
# GRAYSCALE
# ─────────────────────────────────────────────────────────────────────────────

@register("grayscale_image")
def grayscale_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "grayscale_image", "Pillow")
    img = _open_image(ctx.input_path).convert("L")
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# ENHANCE
# ─────────────────────────────────────────────────────────────────────────────

@register("enhance_image")
def enhance_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "enhance_image", "Pillow")
    brightness = float(ctx.params.get("brightness", 1.0))
    contrast   = float(ctx.params.get("contrast",   1.0))
    saturation = float(ctx.params.get("saturation", 1.0))
    sharpness  = float(ctx.params.get("sharpness",  1.0))

    img = _open_image(ctx.input_path)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if sharpness != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(sharpness)

    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img, ctx.output_path, ext)
    return {
        "brightness": brightness, "contrast": contrast,
        "saturation": saturation, "sharpness": sharpness,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WATERMARK
# ─────────────────────────────────────────────────────────────────────────────

@register("watermark_image")
def watermark_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "watermark_image", "Pillow")
    text     = ctx.params.get("text", "CONFIDENTIAL")
    opacity  = int(float(ctx.params.get("opacity", 0.4)) * 255)
    position = ctx.params.get("position", "center")
    fontsize = int(ctx.params.get("fontsize", 36))

    img  = _open_image(ctx.input_path).convert("RGBA")
    ow, oh = img.size

    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw    = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                  fontsize)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw   = bbox[2] - bbox[0]
    th   = bbox[3] - bbox[1]

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
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(watermarked, ctx.output_path, ext)
    return {"watermark_text": text}


# ─────────────────────────────────────────────────────────────────────────────
# ADD TEXT
# ─────────────────────────────────────────────────────────────────────────────

@register("add_text_image")
def add_text_image(ctx: JobContext) -> dict:
    _require(PIL_OK, "add_text_image", "Pillow")
    text     = ctx.params.get("text", "")
    x        = int(ctx.params.get("x", 10))
    y        = int(ctx.params.get("y", 10))
    fontsize = int(ctx.params.get("fontsize", 24))
    color    = ctx.params.get("color", "#000000")
    if not text:
        raise ValidationError("text parameter is required")

    img  = _open_image(ctx.input_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                  fontsize)
    except Exception:
        font = ImageFont.load_default()

    draw.text((x, y), text, font=font, fill=color)
    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "JPEG"
    _save(img.convert("RGB"), ctx.output_path, ext)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE TO PDF
# ─────────────────────────────────────────────────────────────────────────────

@register("image_to_pdf")
def image_to_pdf(ctx: JobContext) -> dict:
    _require(PIL_OK, "image_to_pdf", "Pillow")
    img = _open_image(ctx.input_path).convert("RGB")
    img.save(ctx.output_path, "PDF", resolution=150)
    return {}


@register("images_to_pdf")
def images_to_pdf(ctx: JobContext) -> dict:
    _require(PIL_OK, "images_to_pdf", "Pillow")
    if not ctx.input_paths:
        raise ValidationError("No input images provided")

    images = []
    for p in ctx.input_paths:
        try:
            images.append(_open_image(p).convert("RGB"))
        except Exception as ex:
            log.warning(f"Skipping {p}: {ex}")

    if not images:
        raise ProcessingError("No valid images could be opened")

    images[0].save(
        ctx.output_path, "PDF", save_all=True,
        append_images=images[1:], resolution=150,
    )
    return {"pages": len(images)}


# ─────────────────────────────────────────────────────────────────────────────
# REMOVE BACKGROUND
# ─────────────────────────────────────────────────────────────────────────────

@register("remove_bg")
def remove_bg(ctx: JobContext) -> dict:
    _require(REMBG_OK, "remove_bg", "rembg")
    with open(ctx.input_path, "rb") as f:
        data = f.read()
    result = rembg_remove(data)
    with open(ctx.output_path, "wb") as f:
        f.write(result)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# MERGE IMAGES (side-by-side or vertical stack)
# ─────────────────────────────────────────────────────────────────────────────

@register("merge_images")
def merge_images(ctx: JobContext) -> dict:
    _require(PIL_OK, "merge_images", "Pillow")
    if not ctx.input_paths:
        raise ValidationError("No input images provided")

    direction = ctx.params.get("direction", "horizontal").lower()
    images    = [_open_image(p).convert("RGBA") for p in ctx.input_paths]

    if direction == "horizontal":
        total_w = sum(img.width for img in images)
        max_h   = max(img.height for img in images)
        canvas  = Image.new("RGBA", (total_w, max_h), (255, 255, 255, 255))
        x_off   = 0
        for img in images:
            canvas.paste(img, (x_off, 0))
            x_off += img.width
    else:
        max_w   = max(img.width for img in images)
        total_h = sum(img.height for img in images)
        canvas  = Image.new("RGBA", (max_w, total_h), (255, 255, 255, 255))
        y_off   = 0
        for img in images:
            canvas.paste(img, (0, y_off))
            y_off += img.height

    ext = os.path.splitext(ctx.output_path)[1].lstrip(".").upper() or "PNG"
    _save(canvas.convert("RGB"), ctx.output_path, ext)
    return {"merged_count": len(images), "direction": direction}
