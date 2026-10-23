from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from autocut_api.detection import FrameDetectionUncertain, detect_frame_box


def test_detects_variable_left_and_right_strips_without_top_or_bottom() -> None:
    image = Image.new("RGB", (200, 160), (140, 140, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 17, 159), fill=(10, 20, 70))
    draw.rectangle((180, 0, 199, 159), fill=(10, 20, 70))

    assert detect_frame_box(image) == (18, 0, 180, 160)


def test_detects_adjacent_top_and_right_strips_with_different_colors() -> None:
    image = Image.new("RGB", (200, 160), (140, 140, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 199, 14), fill=(220, 30, 40))
    draw.rectangle((180, 0, 199, 159), fill=(30, 180, 60))

    assert detect_frame_box(image) == (0, 15, 180, 160)


def test_single_candidate_edge_is_not_enough_to_cut_poster_content() -> None:
    image = Image.new("RGB", (200, 160), (140, 140, 140))
    ImageDraw.Draw(image).rectangle((0, 0, 199, 14), fill=(220, 30, 40))

    with pytest.raises(FrameDetectionUncertain):
        detect_frame_box(image)


def test_prefers_outer_rectangle_over_a_strong_line_inside_poster() -> None:
    image = Image.new("RGB", (400, 500), (8, 12, 55))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 369, 469), fill=(220, 120, 20))
    draw.rectangle((30, 440, 369, 449), fill=(255, 235, 160))

    assert detect_frame_box(image) == (30, 30, 370, 470)
