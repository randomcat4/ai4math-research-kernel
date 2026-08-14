"""Strict UTF-8 text extraction."""

from rk.product.material_extractors.base import ExtractedMaterial, ExtractionFailure, project_text


class TextExtractor:
    profile_id = "text_utf8_v1"
    material_kind = "TEXT"
    parser_name = "rk-utf8"
    parser_build = "rk-utf8-v1"

    def extract(self, data: bytes) -> ExtractedMaterial:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtractionFailure("text material is not UTF-8") from error
        return project_text(text, formula_origin="TEXT_FORMULA_CANDIDATE")


__all__ = ["TextExtractor"]
