import pytest
from PIL import Image
from pillow_heif import register_heif_opener

from spotly.image_worker import convert_image

register_heif_opener()


@pytest.mark.parametrize(
    "fmt,extension",
    [
        ("JPEG", "jpg"),
        ("PNG", "png"),
        ("HEIF", "heic"),
        ("WEBP", "webp"),
        ("AVIF", "avif"),
        ("TIFF", "tiff"),
    ],
)
def test_real_formats_produce_browser_previews(tmp_path, fmt, extension):
    source = tmp_path / f"source.{extension}"
    Image.new("RGB", (80, 50), "#ff9900").save(source, format=fmt)
    target = tmp_path / "result"
    result = convert_image(source, target)
    assert (result["width"], result["height"]) == (80, 50)
    assert Image.open(target / "preview.webp").format == "WEBP"
    assert Image.open(target / "thumbnail.webp").size == (80, 50)
    assert source.is_file()


def test_corrupt_file_and_raw_are_rejected(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(ValueError):
        convert_image(bad, tmp_path / "bad-output")
    raw = tmp_path / "image.dng"
    Image.new("RGB", (5, 5)).save(raw, format="TIFF")
    with pytest.raises(ValueError):
        convert_image(raw, tmp_path / "raw-output")


def test_over_100_megapixels_is_rejected_before_decode(tmp_path):
    source = tmp_path / "large.png"
    Image.new("1", (10001, 10000)).save(source)
    with pytest.raises(ValueError):
        convert_image(source, tmp_path / "large-output")


def test_preview_resizes_without_modifying_original(tmp_path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (3000, 2000), "blue").save(source)
    original = source.read_bytes()
    convert_image(source, tmp_path / "out")
    assert Image.open(tmp_path / "out/preview.webp").size == (2560, 1707)
    assert Image.open(tmp_path / "out/thumbnail.webp").size == (512, 341)
    assert source.read_bytes() == original
