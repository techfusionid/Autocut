"""Color-independent detection of supported boundaries near image edges."""

from __future__ import annotations

from itertools import combinations

from PIL import Image

MAX_SCAN_EDGE = 512
MIN_EDGE_STRENGTH = 20.0
MIN_EDGE_SUPPORT = 0.55
MIN_FRAME_SPAN = 0.50
MIN_INSET = 0.01
MAX_EDGE_INSET = 0.35
MIN_BOUNDARY_QUALITY = MIN_EDGE_STRENGTH * MIN_EDGE_SUPPORT


class FrameDetectionUncertain(Exception):
    """Raised when strong, opposing rectangular boundaries are not clear."""


def detect_frame_box(image: Image.Image) -> tuple[int, int, int, int]:
    """Locate an inset crop from independently measured image edges.

    The image is reduced to a small RGB thumbnail. Candidate horizontal and
    vertical boundaries are ranked by contrast strength and the share of the
    scan line that supports the edge. Two supported boundaries must agree,
    either on opposing sides or adjacent sides. This allows a strip on only
    two sides without assuming a fixed strip color or pixel thickness. A
    single strong line is not enough because it may be part of the poster.
    """
    original_width, original_height = image.size
    thumbnail = image.convert("RGB")
    thumbnail.thumbnail((MAX_SCAN_EDGE, MAX_SCAN_EDGE), Image.Resampling.BILINEAR)
    scan_width, scan_height = thumbnail.size
    try:
        pixels = thumbnail.load()
        vertical = _edge_candidates(
            _scan_edges(pixels, scan_width, scan_height, vertical=True), scan_width
        )
        horizontal = _edge_candidates(
            _scan_edges(pixels, scan_width, scan_height, vertical=False), scan_height
        )
        boundaries = _best_supported_boundaries(vertical, horizontal, scan_width, scan_height)
    finally:
        thumbnail.close()

    left = boundaries.get("left", 0)
    top = boundaries.get("top", 0)
    right = boundaries.get("right", scan_width)
    bottom = boundaries.get("bottom", scan_height)
    box = (
        round(left * original_width / scan_width),
        round(top * original_height / scan_height),
        round(right * original_width / scan_width),
        round(bottom * original_height / scan_height),
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise FrameDetectionUncertain
    return box


def _scan_edges(pixels, width: int, height: int, *, vertical: bool) -> list[tuple[float, float]]:
    axis_length = width if vertical else height
    line_length = height if vertical else width
    edges: list[tuple[float, float]] = []
    for position in range(1, axis_length):
        total_strength = 0
        supported_samples = 0
        for offset in range(line_length):
            before = pixels[position - 1, offset] if vertical else pixels[offset, position - 1]
            after = pixels[position, offset] if vertical else pixels[offset, position]
            strength = max(abs(before[channel] - after[channel]) for channel in range(3))
            total_strength += strength
            if strength >= MIN_EDGE_STRENGTH:
                supported_samples += 1
        edges.append((total_strength / line_length, supported_samples / line_length))
    return edges


def _edge_candidates(
    edges: list[tuple[float, float]], axis_length: int
) -> list[tuple[int, float, float]]:
    """Return strong local edge peaks away from the corresponding image edge."""
    peaks: list[tuple[int, float, float]] = []
    for index, (strength, support) in enumerate(edges, start=1):
        if strength < MIN_EDGE_STRENGTH or support < MIN_EDGE_SUPPORT:
            continue
        previous = edges[index - 2][0] if index > 1 else -1
        following = edges[index][0] if index < len(edges) else -1
        if strength >= previous and strength >= following:
            peaks.append((index, strength, support))

    inset_limit = round(axis_length * MIN_INSET)
    max_inset = round(axis_length * MAX_EDGE_INSET)
    return [
        edge
        for edge in peaks
        if inset_limit <= edge[0] <= max_inset
        or axis_length - max_inset <= edge[0] <= axis_length - inset_limit
    ]


def _best_supported_boundaries(
    vertical: list[tuple[int, float, float]],
    horizontal: list[tuple[int, float, float]],
    width: int,
    height: int,
) -> dict[str, int]:
    side_candidates = {
        "left": [edge for edge in vertical if edge[0] <= round(width * MAX_EDGE_INSET)],
        "right": [edge for edge in vertical if edge[0] >= width - round(width * MAX_EDGE_INSET)],
        "top": [edge for edge in horizontal if edge[0] <= round(height * MAX_EDGE_INSET)],
        "bottom": [edge for edge in horizontal if edge[0] >= height - round(height * MAX_EDGE_INSET)],
    }

    rectangles: list[tuple[float, float, dict[str, int]]] = []
    for left in side_candidates["left"]:
        for right in side_candidates["right"]:
            if right[0] - left[0] < width * MIN_FRAME_SPAN:
                continue
            for top in side_candidates["top"]:
                for bottom in side_candidates["bottom"]:
                    if bottom[0] - top[0] < height * MIN_FRAME_SPAN:
                        continue
                    edge_quality = min(
                        left[1] * left[2],
                        right[1] * right[2],
                        top[1] * top[2],
                        bottom[1] * bottom[2],
                    )
                    area = ((right[0] - left[0]) / width) * (
                        (bottom[0] - top[0]) / height
                    )
                    # When multiple strong rectangles are nested, prefer the
                    # outer frame over a strong line inside the poster.
                    score = edge_quality * area**2
                    rectangles.append(
                        (
                            score,
                            edge_quality,
                            {
                                "left": left[0],
                                "right": right[0],
                                "top": top[0],
                                "bottom": bottom[0],
                            },
                        )
                    )

    if rectangles:
        rectangles.sort(key=lambda candidate: candidate[0], reverse=True)
        best_score, best_quality, best = rectangles[0]
        if best_quality < MIN_BOUNDARY_QUALITY:
            raise FrameDetectionUncertain
        for alternative_score, _quality, alternative in rectangles[1:]:
            if alternative_score < best_score * 0.9:
                break
            if not _contains(best, alternative) and not _contains(alternative, best):
                raise FrameDetectionUncertain
        return best

    candidates: list[tuple[float, dict[str, int]]] = []
    for first_side, second_side in combinations(side_candidates, 2):
        if {first_side, second_side} in ({"left", "right"}, {"top", "bottom"}):
            continue
        for first in side_candidates[first_side]:
            for second in side_candidates[second_side]:
                candidates.append(
                    (_pair_quality(first, second), {first_side: first[0], second_side: second[0]})
                )

    # Opposing boundaries should enclose a substantial central image area;
    # this rejects nearby decorative lines that do not resemble edge strips.
    for first_side, second_side, axis_length in (
        ("left", "right", width),
        ("top", "bottom", height),
    ):
        for first in side_candidates[first_side]:
            for second in side_candidates[second_side]:
                if second[0] - first[0] < axis_length * MIN_FRAME_SPAN:
                    continue
                candidates.append(
                    (_pair_quality(first, second, span=(second[0] - first[0]) / axis_length),
                     {first_side: first[0], second_side: second[0]})
                )

    if not candidates:
        raise FrameDetectionUncertain
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    best_quality, best_boundaries = candidates[0]
    if best_quality < MIN_BOUNDARY_QUALITY:
        raise FrameDetectionUncertain

    for alternative_quality, alternative_boundaries in candidates[1:]:
        if alternative_quality < best_quality * 0.9:
            break
        if set(best_boundaries) != set(alternative_boundaries):
            raise FrameDetectionUncertain
        changed_boundary = any(
            side in best_boundaries
            and side in alternative_boundaries
            and abs(best_boundaries[side] - alternative_boundaries[side])
            > max(2, round((width if side in {"left", "right"} else height) * 0.02))
            for side in set(best_boundaries) | set(alternative_boundaries)
        )
        if changed_boundary:
            raise FrameDetectionUncertain
    return best_boundaries


def _contains(outer: dict[str, int], inner: dict[str, int]) -> bool:
    """Return whether one candidate crop box contains another candidate box."""
    return (
        outer.get("left", 0) <= inner.get("left", 0)
        and outer.get("top", 0) <= inner.get("top", 0)
        and outer.get("right", 10**9) >= inner.get("right", 10**9)
        and outer.get("bottom", 10**9) >= inner.get("bottom", 10**9)
    )


def _pair_quality(
    first: tuple[int, float, float],
    second: tuple[int, float, float],
    *,
    span: float | None = None,
) -> float:
    first_score = first[1] * first[2]
    second_score = second[1] * second[2]
    weaker_edge = min(first_score, second_score)
    balanced_strength = min(first[1], second[1]) / max(first[1], second[1])
    if span is None:
        return weaker_edge * (0.8 + 0.2 * balanced_strength)
    return weaker_edge * (0.7 + 0.2 * balanced_strength + 0.1 * span)
