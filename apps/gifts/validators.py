"""Validate decoded raster uploads; never trust filenames or content types."""
from pathlib import Path
import warnings
from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4096 * 4096
EXTENSIONS = {'PNG': {'.png'}, 'JPEG': {'.jpg', '.jpeg'}, 'WEBP': {'.webp'}}


def validate_gift_image(value):
    if not value:
        return
    try:
        if value.size > MAX_BYTES:
            raise ValueError('图片超过2MiB')
        value.open('rb')
        value.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(value)
            if image.format not in EXTENSIONS or Path(value.name).suffix.lower() not in EXTENSIONS[image.format]:
                raise ValueError('仅支持PNG/JPEG/WebP')
            if image.width * image.height > MAX_PIXELS or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('图片尺寸过大或为动画')
            image.verify()
            value.seek(0)
            Image.open(value).load()
        value.seek(0)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError('图片无效：需要不超过2MiB的安全静态PNG/JPEG/WebP') from exc
