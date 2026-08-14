"""TeX source extraction preserving exact formula spans."""

from rk.product.material_extractors.base import ExtractedMaterial, ExtractionFailure, project_tex


class TexExtractor:
    profile_id = "tex_source_v1"
    material_kind = "TEX"
    parser_name = "rk-tex-source"
    parser_build = "rk-tex-source-v1"

    def extract(self, data: bytes) -> ExtractedMaterial:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtractionFailure("TeX material is not UTF-8") from error
        return project_tex(text)


__all__ = ["TexExtractor"]
