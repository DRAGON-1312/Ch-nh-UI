from __future__ import annotations

from typing import Iterable, List, Sequence, Dict, Tuple
import math

# Schemas (project layer)
from app.schemas.common import Provider, Tag
from app.schemas.location import NormalizedOrigin, GeoPoint
from app.schemas.place import PlaceCandidate as SPlaceCandidate, ProviderIds

# Providers (SerpAPI)
from app.providers.serpapi.nearby import nearby_search as serp_nearby
from app.providers.serpapi.nearby import PlaceCandidate as SerpSeed
from app.providers.serpapi.enrich import light_enrich as serp_light_enrich


# ---- helper tạo id ổn định ----
def _make_cand_id(provider: Provider, serp_place_id: str | None, lat: float, lng: float) -> str:
    if serp_place_id:
        return f"{provider.name.lower()}:{serp_place_id}"
    # fallback – rất hiếm khi SerpAPI không trả place_id
    return f"geo:{lat:.5f},{lng:.5f}"


# ---------------- Geo utils ----------------
def _haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Khoảng cách đường chim bay (m)."""
    R = 6371008.8
    lat1, lon1, lat2, lon2 = map(math.radians, [a.lat, a.lng, b.lat, b.lng])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(h))


# ------------- Tag -> query/type mapping (SerpAPI Google Maps) -------------
_GOOGLE_TYPES: Dict[Tag, str] = {
    Tag.RESTAURANT: "restaurant",
    Tag.CAFE: "cafe",
    Tag.HOTEL: "hotel",
    # mở rộng dần khi cần
}

def _serp_tags(tags: Iterable[Tag]) -> Sequence[str]:
    out: List[str] = []
    for t in tags or []:
        v = _GOOGLE_TYPES.get(t)
        if v:
            out.append(v)
    if not out:
        out = ["restaurant"]            # sensible default
    return (out[0],)                    # wrapper hiện dùng 1 type chính → lấy tag đầu


# ---------------- Safe builder ----------------
def _safe_place_candidate(**kwargs) -> SPlaceCandidate:
    """
    Chỉ giữ lại field mà SPlaceCandidate thật sự có (tránh vỡ khi schema đổi nhẹ).
    """
    allowed = set(getattr(SPlaceCandidate, "model_fields", {}).keys() or [])
    data = {k: v for k, v in kwargs.items() if k in allowed}
    return SPlaceCandidate(**data)


# ---------------- Public API ----------------
async def nearby_search(
    *,
    origin: NormalizedOrigin,
    tags: List[Tag],
    radius_m: int = 2000,
    limit: int = 20,                 # dùng ở Phase B (Ranking) để cắt 20
    lang: str = "vi",
    country: str = "vn",
    provider: Provider = Provider.SERPAPI,
    enrich_missing_signals: bool = True,
    # 2-phase trim controls
    seed_cap: int = 30,              # Phase A: số seed tối đa đẩy qua Matrix
    sort_by_distance: bool = True,   # True = ưu tiên gần trước khi cắt 30
) -> List[SPlaceCandidate]:
    """
    Lấy POI quanh origin bằng SerpAPI → chuẩn hoá PlaceCandidate.
    KHÔNG ranking, KHÔNG ETA. (Phase B sẽ làm Matrix + Ranking)
    """
    if provider != Provider.SERPAPI:
        raise NotImplementedError("Nearby currently supports SerpAPI only.")

    assert origin and origin.lat is not None and origin.lng is not None
    origin_pt = GeoPoint(lat=origin.lat, lng=origin.lng)

    # 1) gọi SerpAPI (lấy dư một chút để lọc + cắt)
    seeds: List[SerpSeed] = await serp_nearby(
        origin_lat=origin.lat,
        origin_lng=origin.lng,
        radius_km=max(0.1, float(radius_m) / 1000.0),
        tags=_serp_tags(tags),
        language=lang,
        country=country,
        page_size=max(seed_cap * 2, limit * 2, 40),
    )
    if not seeds:
        return []

    # 2) enrich nhẹ (best-effort)
    if enrich_missing_signals:
        try:
            seeds = await serp_light_enrich(seeds, language=lang, country=country)
        except Exception:
            pass

    # 3) chuẩn hoá + tính distance_m để pre-trim
    tmp: List[Tuple[int, SPlaceCandidate]] = []
    for s in seeds:
        try:
            lat = float(getattr(s, "lat"))
            lng = float(getattr(s, "lng"))
        except Exception:
            continue

        d_m = int(_haversine_m(origin_pt, GeoPoint(lat=lat, lng=lng)))
        if radius_m and d_m > radius_m:
            continue

        # provider ids
        serp_pid = getattr(s, "place_id", None)
        pids = ProviderIds(serpapi_place_id=serp_pid)

        # id bắt buộc cho PlaceCandidate
        cand_id = _make_cand_id(Provider.SERPAPI, serp_pid, lat, lng)

        # build ứng viên
        cand = _safe_place_candidate(
            id=cand_id,                              # id ổn định (dùng cho Matrix/Ranking)
            provider=Provider.SERPAPI,
            provider_ids=pids,
            name=getattr(s, "name", "") or getattr(s, "title", "") or "",
            display_address=getattr(s, "address", None),
            location=GeoPoint(lat=lat, lng=lng),
            tags=list(tags or []),
            rating=getattr(s, "rating", None),
            user_ratings_total=getattr(s, "user_ratings_total", None),
            distance_m=d_m,
        )
        tmp.append((d_m, cand))

    # 4) Phase A: pre-trim về tối đa seed_cap
    if sort_by_distance:
        tmp.sort(key=lambda t: t[0])  # gần → xa

    return [c for _, c in tmp[: max(1, seed_cap)]]
