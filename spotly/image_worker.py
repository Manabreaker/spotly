import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 100_000_000
ALLOWED = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF", "GIF", "BMP", "TIFF"}
RAW = {".dng", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}


def convert_image(source, target):
    source, target = Path(source), Path(target)
    if source.suffix.lower() in RAW or source.stat().st_size > 30_000_000:
        raise ValueError("Файл больше 30 МБ или имеет неподдерживаемый формат RAW")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if (
                    image.format not in ALLOWED
                    or image.width * image.height > 100_000_000
                    or 50706 in image.getexif()
                ):
                    raise ValueError("Неподдерживаемый формат или размер больше 100 мегапикселей")
                info = {
                    "width": image.width,
                    "height": image.height,
                    "media_type": Image.MIME.get(image.format, "image/heic"),
                }
                image.seek(getattr(image, "primary_index", 0))
                image.load()
                converted = ImageOps.exif_transpose(image)
                profile = converted.info.get("icc_profile")
                if profile:
                    try:
                        from io import BytesIO

                        converted = ImageCms.profileToProfile(
                            converted,
                            ImageCms.ImageCmsProfile(BytesIO(profile)),
                            ImageCms.createProfile("sRGB"),
                            outputMode="RGB",
                        )
                    except ImageCms.PyCMSError, OSError, ValueError:
                        converted = converted.convert("RGB")
                elif converted.mode not in ("RGB", "RGBA"):
                    converted = converted.convert("RGB")
                target.mkdir(parents=True, exist_ok=True)
                for name, bound in (("preview", 2560), ("thumbnail", 512)):
                    copy = converted.copy()
                    copy.thumbnail((bound, bound), Image.Resampling.LANCZOS)
                    copy.save(target / f"{name}.webp", "WEBP", quality=85, exif=b"")
                return info
    except (
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
    ) as exc:
        raise ValueError(
            "Не удалось прочитать фотографию. Проверьте формат и целостность файла"
        ) from exc


if __name__ == "__main__":
    try:
        print(json.dumps(convert_image(sys.argv[1], sys.argv[2])))
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
