"""Local OCR for explicitly selected photo imports."""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


class PhotoOcrError(ValueError):
    """The selected image could not be decoded or read by local OCR."""


SUPPORTED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
MAX_IMAGE_PIXELS = 40_000_000


def extract_text(image_bytes: bytes, filename: str) -> str:
    if not shutil.which("tesseract"):
        raise RuntimeError("Tesseract OCR is not installed. Install it locally to import photos.")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_PHOTO_EXTENSIONS:
        raise PhotoOcrError("Use a JPG, PNG, WebP, TIFF, BMP, or HEIC photo.")

    with tempfile.TemporaryDirectory(prefix="iorganizer-photo-") as temp_dir:
        source = Path(temp_dir) / ("selected" + suffix)
        normalized = Path(temp_dir) / "selected.png"
        source.write_bytes(image_bytes)

        if suffix in {".heic", ".heif"}:
            sips = shutil.which("sips")
            if not sips:
                raise PhotoOcrError("HEIC conversion is unavailable here. Export the photo as JPEG and try again.")
            try:
                converted = subprocess.run(
                    [sips, "-s", "format", "png", str(source), "--out", str(normalized)],
                    capture_output=True, timeout=20, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PhotoOcrError("HEIC conversion took too long. Try exporting the photo as JPEG.") from exc
            if converted.returncode != 0:
                raise PhotoOcrError("Could not decode this HEIC photo. Try exporting it as JPEG.")
            try:
                with Image.open(normalized) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise PhotoOcrError("This photo is too large to process safely.")
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise PhotoOcrError("This HEIC photo could not be prepared for text recognition.") from exc
        else:
            try:
                with Image.open(io.BytesIO(image_bytes)) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise PhotoOcrError("This photo is too large to process safely.")
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.save(normalized, format="PNG")
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise PhotoOcrError("This image file could not be opened safely.") from exc

        try:
            result = subprocess.run(
                [shutil.which("tesseract") or "tesseract", str(normalized), "stdout", "-l", "eng", "--psm", "6"],
                capture_output=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PhotoOcrError("Photo text recognition took too long. Try a smaller image.") from exc
        if result.returncode != 0:
            raise PhotoOcrError("Text recognition could not read this photo. Try a clearer image.")
        return result.stdout.decode("utf-8", errors="replace").strip()
