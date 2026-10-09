from datetime import date
from math import inf
from typing import Any, Literal

from shapely.errors import GEOSException
from shapely.geometry import box, mapping, shape

from ..schemas.observation_pairs import (
    Compatibility,
    Coverage,
    ObservationPairCandidate,
    PairVerificationStatus,
)
from ..schemas.observations import ObservationMetadata

DESIRED_SEPARATION_DAYS = 12.0
TEMPORAL_RANKING_PRECISION_HOURS = 0.1


def _product_compatibility(
    pre: ObservationMetadata,
    post: ObservationMetadata,
) -> tuple[Compatibility, list[str]]:
    if pre.collection != "sentinel-1-grd" or post.collection != "sentinel-1-grd":
        return "mismatch", ["Both items must belong to the Sentinel-1 GRD collection."]

    product_types = (pre.product_type, post.product_type)
    if any(value is None for value in product_types):
        return "unknown", ["Product type metadata is missing for at least one observation."]
    if not all("GRDH" in value.upper() for value in product_types if value is not None):
        return "mismatch", ["At least one product type is not a Sentinel-1 GRD high-resolution product."]
    return "match", ["Both observations are Sentinel-1 GRD products."]


def _temporal_compatibility(
    pre: ObservationMetadata,
    post: ObservationMetadata,
    event_date: date,
) -> tuple[Compatibility, float | None, list[str]]:
    pre_time = pre.acquisition_start
    post_time = post.acquisition_start
    if pre_time is None or post_time is None:
        return "unknown", None, ["Acquisition start time is missing for at least one observation."]

    elapsed_seconds = (post_time - pre_time).total_seconds()
    pre_is_before = pre_time.date() < event_date
    post_is_after = post_time.date() > event_date
    if not pre_is_before or not post_is_after or elapsed_seconds <= 0:
        reasons = [
            "The pair does not strictly bracket the event date; event-day acquisitions are treated as ambiguous."
        ]
        return "mismatch", elapsed_seconds, reasons
    return "match", elapsed_seconds, ["Acquisitions fall strictly before and after the event date."]


def _orbit_compatibility(
    pre: ObservationMetadata,
    post: ObservationMetadata,
) -> tuple[Compatibility, list[str]]:
    comparisons: list[tuple[Any, Any]] = [
        (pre.orbit_direction, post.orbit_direction),
        (pre.relative_orbit, post.relative_orbit),
    ]
    if any(left is not None and right is not None and left != right for left, right in comparisons):
        return "mismatch", ["Orbit direction or relative orbit differs between observations."]
    if any(left is None or right is None for left, right in comparisons):
        return "unknown", ["Orbit direction or relative-orbit metadata is missing for at least one observation."]
    return "match", ["Orbit direction and relative orbit match."]


def _polarization_compatibility(
    pre: ObservationMetadata,
    post: ObservationMetadata,
) -> tuple[Compatibility, list[str]]:
    if pre.polarization is None or post.polarization is None:
        return "unknown", ["Polarization metadata is missing for at least one observation."]
    if set(pre.polarization) != set(post.polarization):
        return "mismatch", ["Available polarization channels differ between observations."]
    return "match", ["Polarization channels match."]


def _read_geometry(
    observation: ObservationMetadata,
) -> tuple[Any | None, str | None]:
    if observation.footprint is None:
        return None, f"Footprint geometry is missing for {observation.scene_id}."
    try:
        geometry = shape(observation.footprint)
    except (TypeError, ValueError, KeyError) as exc:
        return None, f"Footprint geometry is invalid for {observation.scene_id}: {exc}"
    if geometry.is_empty or not geometry.is_valid:
        return None, f"Footprint geometry is empty or invalid for {observation.scene_id}."
    return geometry, None


def _geometry_coverage(
    geometry: Any | None,
    aoi: Any,
) -> tuple[Coverage, Any | None]:
    if geometry is None:
        return "unknown", None
    try:
        intersection = geometry.intersection(aoi)
        if geometry.covers(aoi):
            return "full", intersection
        if not intersection.is_empty and intersection.area > 0:
            return "partial", intersection
        return "none", intersection
    except GEOSException:
        return "unknown", None


def _pair_verification_status(
    product: Compatibility,
    temporal: Compatibility,
    orbit: Compatibility,
    polarization: Compatibility,
    pre_coverage: Coverage,
    post_coverage: Coverage,
    common_coverage: Coverage,
) -> PairVerificationStatus:
    if product == "mismatch" or temporal == "mismatch" or common_coverage == "none":
        return "unsuitable"
    if product == "unknown" or temporal == "unknown" or common_coverage == "unknown":
        return "not_verified"
    if pre_coverage == "none" or post_coverage == "none":
        return "unsuitable"
    if (
        common_coverage == "partial"
        or orbit == "mismatch"
        or polarization == "mismatch"
        or pre_coverage != "full"
        or post_coverage != "full"
    ):
        return "questionable"
    if orbit == "unknown" or polarization == "unknown":
        return "not_verified"
    return "compatible"


def _ranking_key(candidate: ObservationPairCandidate) -> tuple[Any, ...]:
    status_order: dict[PairVerificationStatus, int] = {
        "compatible": 0,
        "questionable": 1,
        "not_verified": 2,
        "unsuitable": 3,
    }
    coverage_order: dict[Coverage, int] = {"full": 0, "partial": 1, "unknown": 2, "none": 3}
    compatibility_order: dict[Compatibility, int] = {"match": 0, "unknown": 1, "mismatch": 2}
    if candidate.elapsed_days is None:
        temporal_distance = inf
    else:
        temporal_distance = round(
            abs(candidate.elapsed_days - DESIRED_SEPARATION_DAYS)
            * 24
            / TEMPORAL_RANKING_PRECISION_HOURS
        )
    return (
        status_order[candidate.verification_status],
        coverage_order[candidate.common_aoi_coverage],
        compatibility_order[candidate.orbit_compatibility],
        compatibility_order[candidate.polarization_compatibility],
        temporal_distance,
        candidate.pre_scene_id,
        candidate.post_scene_id,
    )


def _evaluate_pair(
    pre: ObservationMetadata,
    post: ObservationMetadata,
    event_date: date,
    aoi: Any,
) -> ObservationPairCandidate:
    product_status, product_reasons = _product_compatibility(pre, post)
    temporal_status, elapsed_seconds, temporal_reasons = _temporal_compatibility(
        pre, post, event_date
    )
    orbit_status, orbit_reasons = _orbit_compatibility(pre, post)
    polarization_status, polarization_reasons = _polarization_compatibility(pre, post)

    pre_geometry, pre_geometry_warning = _read_geometry(pre)
    post_geometry, post_geometry_warning = _read_geometry(post)
    pre_coverage, _ = _geometry_coverage(pre_geometry, aoi)
    post_coverage, _ = _geometry_coverage(post_geometry, aoi)

    warnings: list[str] = []
    for warning in (pre_geometry_warning, post_geometry_warning):
        if warning is not None:
            warnings.append(warning)

    common_coverage: Coverage = "unknown"
    common_intersection: dict[str, Any] | None = None
    if pre_geometry is not None and post_geometry is not None:
        try:
            shared_geometry = pre_geometry.intersection(post_geometry)
            shared_aoi = shared_geometry.intersection(aoi)
            if shared_aoi.is_empty or shared_aoi.area == 0:
                common_coverage = "none"
                common_intersection = None
            else:
                common_intersection = mapping(shared_aoi)
                common_coverage = "full" if shared_geometry.covers(aoi) else "partial"
        except GEOSException:
            warnings.append("Could not calculate the shared footprint intersection.")

    if elapsed_seconds is None:
        elapsed_days = None
    else:
        elapsed_days = elapsed_seconds / 86400.0

    reasons = product_reasons + temporal_reasons + orbit_reasons + polarization_reasons
    if common_coverage == "full":
        reasons.append("The common footprint intersection covers the configured AOI.")
    elif common_coverage == "partial":
        reasons.append("The common footprint intersection covers only part of the configured AOI.")
    elif common_coverage == "none":
        reasons.append("The observation footprints have no positive-area intersection with the configured AOI.")
    else:
        warnings.append("AOI coverage could not be verified from both footprint geometries.")

    if elapsed_days is not None:
        reasons.append(
            f"Acquisition separation is {elapsed_days:.6f} days; ranking prefers, but does not require, 12 days and compares closeness in {TEMPORAL_RANKING_PRECISION_HOURS:.1f}-hour bands."
        )

    if product_status == "unknown":
        warnings.append("Product suitability cannot be confirmed from the available product-type metadata.")
    if temporal_status == "unknown":
        warnings.append("Temporal ordering cannot be verified without both acquisition timestamps.")
    if orbit_status == "unknown":
        warnings.extend(orbit_reasons)
    if polarization_status == "unknown":
        warnings.extend(polarization_reasons)

    status = _pair_verification_status(
        product_status,
        temporal_status,
        orbit_status,
        polarization_status,
        pre_coverage,
        post_coverage,
        common_coverage,
    )
    if status == "compatible":
        reasons.append("Metadata and footprint checks support this pair as a candidate for downstream review.")
    elif status == "questionable":
        warnings.append("One or more compatibility checks are partial or mismatched; review before use.")
    elif status == "not_verified":
        warnings.append("Required metadata is incomplete; this pair is not verified for downstream use.")
    else:
        warnings.append("One or more checks indicate this pair is unsuitable for the configured comparison.")

    return ObservationPairCandidate(
        rank=0,
        pre_scene_id=pre.scene_id,
        post_scene_id=post.scene_id,
        elapsed_seconds=elapsed_seconds,
        elapsed_days=elapsed_days,
        product_compatibility=product_status,
        temporal_compatibility=temporal_status,
        orbit_compatibility=orbit_status,
        polarization_compatibility=polarization_status,
        pre_aoi_coverage=pre_coverage,
        post_aoi_coverage=post_coverage,
        common_aoi_coverage=common_coverage,
        common_footprint_intersection=common_intersection,
        verification_status=status,
        ranking_reasons=reasons,
        warnings=warnings,
    )


def rank_observation_pairs(
    pre_observations: list[ObservationMetadata],
    post_observations: list[ObservationMetadata],
    bbox: tuple[float, float, float, float],
    event_date: date,
) -> list[ObservationPairCandidate]:
    """Rank real provider observations by metadata and spatial compatibility.

    The rank is a transparent ordering, not a probability or measure of flood
    detection accuracy. Footprint coverage uses Shapely topological operations
    in the provider's WGS84 GeoJSON coordinates and reports no area percentage.
    """
    aoi = box(*bbox)
    candidates = [
        _evaluate_pair(pre, post, event_date, aoi)
        for pre in pre_observations
        for post in post_observations
    ]
    candidates.sort(key=_ranking_key)
    return [candidate.model_copy(update={"rank": index}) for index, candidate in enumerate(candidates, 1)]
