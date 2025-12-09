from __future__ import annotations

from typing import Optional, List, Tuple
import math
import re

from app.schemas.common import ReasonCode
from app.schemas.location import (
    GeoPoint,
    NormalizedOrigin,
    OriginKind,
    OriginSource,
)
from app.providers.trackasia.geocode import forward_geocode
from app.providers.trackasia.reverse import reverse_geocode

import logging
log = logging.getLogger(__name__)


# =============== Errors & small utils ===============

class OriginIntakeError(Exception):
    """Bad request for origin intake (empty text, invalid input...)."""


def _map_provider_error_to_reason(exc: Exception) -> ReasonCode:
    msg = str(exc).lower()
    if "401" in msg or "unauthorized" in msg or "forbidden" in msg:
        return ReasonCode.PROVIDER_QUOTA
    if "429" in msg or "rate limit" in msg or "quota" in msg:
        return ReasonCode.PROVIDER_QUOTA
    if "timeout" in msg or "timed out" in msg:
        return ReasonCode.TECHNICAL
    if "400" in msg or "422" in msg:
        return ReasonCode.BUSINESS_NO_MATCH
    return ReasonCode.TECHNICAL


_WORD = re.compile(r"[A-Za-zÀ-ỹ0-9]+")


def _tokens(s: str) -> set[str]:
    return set(w.lower() for w in _WORD.findall(s or ""))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _distance_km(a: GeoPoint, b: GeoPoint) -> float:
    R = 6371.0088
    lat1, lon1, lat2, lon2 = map(math.radians, [a.lat, a.lng, b.lat, b.lng])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def _gaussian_weight(d_km: float, sigma_km: float) -> float:
    if sigma_km <= 0:
        return 0.0
    return math.exp(-(d_km ** 2) / (2.0 * sigma_km ** 2))


# =============== Public resolvers ===============

async def resolve_from_latlng(
    lat: float,
    lng: float,
    *,
    source: OriginSource = OriginSource.DEVICE_GEO,   # hoặc MAP_CLICK
    want_reverse_address: bool = True,
) -> tuple[Optional[NormalizedOrigin], ReasonCode]:

    gp = GeoPoint(lat=lat, lng=lng)

    addr = None
    place_id = None
    reason = ReasonCode.OK

    if want_reverse_address:
        try:
            cand = (await reverse_geocode(gp.lat, gp.lng, limit=1)) or []
            if cand:
                addr = cand[0].full_address or cand[0].name
                place_id = cand[0].place_id or None
        except Exception as e:
            reason = _map_provider_error_to_reason(e)

    origin = NormalizedOrigin(
        lat=gp.lat,
        lng=gp.lng,
        kind=OriginKind.POINT,
        place_id=place_id,
        display_address=addr,
        source=source,
        confidence=0.95 if addr else 0.9,
    )
    return origin, reason


async def resolve_from_text_scored(
    query: str,
    *,
    top_k: int = 3,
    alpha: float = 0.8,
    radius_km: Optional[float] = None,
    bias_center: Optional[GeoPoint] = None,
) -> Tuple[Optional[NormalizedOrigin], ReasonCode, bool, List[dict]]:
    q = (query or "").strip()
    if not q:
        raise OriginIntakeError("Empty origin text")

    # Clamp radius tối đa 7km
    if radius_km is not None and radius_km > 7:
        radius_km = 7

    try:
        raw = await forward_geocode(q, limit=max(top_k, 10))
        log.info("origin.debug.geocode_raw q=%r n=%d", q, len(raw))
        for i, r in enumerate(raw, 1):
            log.info(
                "origin.debug.cand[%d] name=%r addr=%r lat=%.6f lng=%.6f",
                i,
                getattr(r, "name", ""),
                getattr(r, "full_address", ""),
                getattr(r, "lat", 0.0),
                getattr(r, "lng", 0.0),
            )
    except Exception as e:
        return None, _map_provider_error_to_reason(e), False, []

    if not raw:
        return None, ReasonCode.BUSINESS_NO_MATCH, False, []

    Tq = _tokens(q)
    sigma = (radius_km / 2.0) if (radius_km and radius_km > 0) else None

    scored: List[Tuple[float, object, float]] = []  # (score, raw_candidate, text_sim)
    admin_types = {"locality", "administrative_area", "city", "district"}

    for r in raw:
        name = getattr(r, "name", "") or ""
        addr = getattr(r, "full_address", "") or ""
        types = getattr(r, "types", []) or []

        text_sim = max(_jaccard(Tq, _tokens(name)), _jaccard(Tq, _tokens(addr)))
        bonus = 0.05 if any(t in admin_types for t in types) else 0.0

        if bias_center and sigma:
            d = _distance_km(bias_center, GeoPoint(lat=getattr(r, "lat"), lng=getattr(r, "lng")))
            g = _gaussian_weight(d, sigma)
            score = alpha * text_sim + (1 - alpha) * g + bonus
        else:
            score = min(1.0, text_sim + bonus)

        scored.append((score, r, text_sim))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    best = top[0][0]
    second = top[1][0] if len(top) > 1 else 0.0

    # Heuristic mơ hồ
    need_confirm = (best < 0.60) or ((best - second) < 0.08)

    # pack candidates lite
    cands_lite = [
        {
            "name": getattr(t[1], "name", ""),
            "display_address": getattr(t[1], "full_address", None),
            "lat": getattr(t[1], "lat"),
            "lng": getattr(t[1], "lng"),
            "place_id": getattr(t[1], "place_id", None),
            "types": getattr(t[1], "types", None),
        }
        for t in top
    ]

    if need_confirm:
        return None, ReasonCode.OK, True, cands_lite

    # Auto-pick best
    best_raw = top[0][1]
    origin = NormalizedOrigin.from_text_geocode(
        lat=getattr(best_raw, "lat"),
        lng=getattr(best_raw, "lng"),
        raw_query=q,
        place_id=getattr(best_raw, "place_id", None),
        display_address=getattr(best_raw, "full_address", None) or getattr(best_raw, "name", ""),
        kind=OriginKind.POINT,
        confidence=min(0.95, best + 0.20),
    )
    return origin, ReasonCode.OK, False, cands_lite


async def intake_origin(
    *,
    origin_text: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    source_hint: Optional[str] = None,  # "device" | "map" | "browser"
    # scoring params (chỉ dùng khi origin_text):
    top_k: int = 3,
    alpha: float = 0.8,
    radius_km: Optional[float] = None,
    bias_center: Optional[GeoPoint] = None,
) -> Tuple[Optional[NormalizedOrigin], ReasonCode, bool, List[dict]]:
    """
    Facade duy nhất cho module:
      - Có (lat,lng) → resolve_from_latlng (không need_confirm, không candidates)
      - Có origin_text → resolve_from_text_scored (có thể need_confirm)
    """
    if lat is not None and lng is not None:
        src = {
            "device": OriginSource.DEVICE_GEO,
            "map": OriginSource.MAP_CLICK,
            "browser": OriginSource.DEVICE_GEO,  # gộp browser vào device
        }.get((source_hint or "").lower(), OriginSource.DEVICE_GEO)
        origin, reason = await resolve_from_latlng(lat, lng, source=src)
        return origin, reason, False, []

    if origin_text:
        return await resolve_from_text_scored(
            origin_text,
            top_k=top_k,
            alpha=alpha,
            radius_km=radius_km,
            bias_center=bias_center,
        )

    return None, ReasonCode.TECHNICAL, False, []
