"""Built-in immutable material extraction profiles."""

from rk.product.material_extractors.base import ExtractedMaterial, ExtractionFailure
from rk.product.material_extractors.image import ImageExtractor
from rk.product.material_extractors.pdf import PdfExtractor
from rk.product.material_extractors.tex import TexExtractor
from rk.product.material_extractors.text import TextExtractor

__all__ = [
    "ExtractedMaterial",
    "ExtractionFailure",
    "ImageExtractor",
    "PdfExtractor",
    "TexExtractor",
    "TextExtractor",
]
