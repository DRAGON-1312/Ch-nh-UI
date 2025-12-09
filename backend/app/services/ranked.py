from __future__ import annotations

import math  
from typing import Dict, List, Optional, Sequence, Tuple

from app.schemas.common import ReasonCode, Tag, Provider
from app.schemas.location import NormalizedOrigin
from app.schemas.place import PlaceCandidate
from app.schemas.ranking import PlaceScored, WeightConfig, BayesianPrior

# Services / Providers đã có sẵn
from app.services.intake_origin import intake_origin
from app.services.poi import nearby_search
from app.providers.trackasia.matrix import matrix_1_to_many
from app.services.ranking import rank_and_cut


# Map mode → TrackAsia profile
_MODE2PROFILE = {
    "driving": "car",
    "motorcycling": "moto",
    "walking": "walk",
    "truck": "truck",
}

# Tốc độ fallback (km/h) khi Matrix lỗi
_FALLBACK_SPEEDS_KMH = {
    "walking": 4.8,
    "bicycling": 14.0,
    "motorcycling": 25.0,   # đô thị
    "driving": 22.0,        # đô thị giờ cao điểm
    "truck": 18.0,
}


def _stable_id(c: PlaceCandidate, idx: int) -> str:
    """
    Lấy id ổn định để ghép kết quả Matrix ↔ Candidate.
    Ưu tiên c.id → provider_ids.serpapi_place_id → index.
    """
    cid = getattr(c, "id", None)
    if cid:
        return str(cid)

    pids = getattr(c, "provider_ids", None)
    sid = getattr(pids, "serpapi_place_id", None) if pids else None
    if sid:
        return f"serpapi:{sid}"

    # fallback cuối: theo index (ít gặp)
    return f"seed:{idx}"


def _hav_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance (m)."""
    R = 6371008.8
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def search_ranked(
    *,
    # ---- Origin inputs (ít nhất 1 cách) ----
    origin_text: Optional[str] = None,
    origin: Optional[NormalizedOrigin] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,

    # ---- Search config ----
    tags: List[Tag],
    radius_m: int = 2000,
    mode: str = "driving",
    seed_cap: int = 30,         # số seed tối đa lấy từ Nearby trước khi qua Matrix/Ranking
    limit: int = 20,            # số kết quả cuối cùng

    # ---- i18n / provider ----
    lang: str = "vi",
    country: str = "vn",

    # ---- Ranking params (tuỳ chọn) ----
    weights: Optional[WeightConfig] = None,
    prior: Optional[BayesianPrior] = None,
):
    """
    Orchestrator end-to-end:
      1) Intake → NormalizedOrigin (có thể trả need_confirm + candidates)
      2) Nearby (seed ≤ seed_cap)
      3) Matrix (ETA + distance) theo `mode` (+ Fallback Haversine nếu Matrix lỗi)
      4) Ranking (ETA-first) → cắt ≤ limit
    Trả về dict: {reason_code, need_confirm, origin|None, items|None, candidates|None}
    """

    # A) Resolve origin (Intake) nếu chưa có
    if origin is None:
        origin, reason, need_confirm, candidates = await intake_origin(
            origin_text=origin_text,
            lat=lat, lng=lng,
            top_k=3,       # số ứng viên trả ra khi mơ hồ
            alpha=0.8,     # như trong intake_req
        )
        if need_confirm:
            # FE hiển thị candidates để user chọn
            return {
                "reason_code": reason,
                "need_confirm": True,
                "origin": None,
                "items": None,
                "candidates": candidates,
            }
    else:
        reason = ReasonCode.OK

    # B) Nearby (chỉ build seeds)
    seeds: List[PlaceCandidate] = await nearby_search(
        origin=origin,
        tags=tags,
        radius_m=radius_m,
        limit=limit,                       # limit cuối vẫn do Ranking cắt; limit ở đây chỉ để an toàn
        lang=lang,
        country=country,
        provider=Provider.SERPAPI,
        enrich_missing_signals=True,
        seed_cap=seed_cap,
        sort_by_distance=True,
    )

    if not seeds:
        return {
            "reason_code": reason,
            "need_confirm": False,
            "origin": origin,
            "items": [],
            "candidates": None,
        }

    # Chuẩn hoá id ổn định để ghép ETA/distance
    for i, s in enumerate(seeds):
        cid = _stable_id(s, i)
        # Nếu model có field id thì set, còn không thì bỏ qua (ranking dùng getattr)
        try:
            if getattr(s, "id", None) != cid:
                setattr(s, "id", cid)
        except Exception:
            pass

    # C) Matrix (lấy cả duration & distance — ranking ưu tiên ETA trước)
    profile = _MODE2PROFILE.get(mode, "car")
    dests: Sequence[Tuple[float, float]] = [
        (s.location.lat, s.location.lng) for s in seeds if s.location
    ]

    od_list = None
    try:
        od_list = await matrix_1_to_many(
            origin_lat=origin.lat,
            origin_lng=origin.lng,
            destinations=dests,
            profile=profile,
            annotations="duration,distance",
        )
    except Exception:
        # Provider lỗi (Cloudflare/5xx/timeout) → dùng fallback
        od_list = None

    # Map về dict theo candidate.id
    durations_s_by_id: Dict[str, int] = {}
    distances_m_by_id: Dict[str, int] = {}

    # Duyệt từng seed và điền số liệu: ưu tiên Matrix, thiếu thì fallback
    speed_kmh = _FALLBACK_SPEEDS_KMH.get(mode, 22.0)

    for idx, s in enumerate(seeds):
        cid = getattr(s, "id", None)
        if not cid or not s.location:
            continue

        dur_s: Optional[int] = None
        dist_m: Optional[int] = None

        # 1) Lấy từ Matrix nếu có
        if od_list and idx < len(od_list):
            od = od_list[idx]
            if od:
                if getattr(od, "duration_s", None) is not None:
                    dur_s = int(od.duration_s)
                if getattr(od, "distance_m", None) is not None:
                    dist_m = int(od.distance_m)

        # 2) Fallback nếu thiếu bất kỳ cái nào
        if dist_m is None:
            dist_m = int(_hav_m(origin.lat, origin.lng, s.location.lat, s.location.lng))
        if dur_s is None and speed_kmh > 0:
            km = float(dist_m) / 1000.0
            dur_s = int((km / speed_kmh) * 3600)

        if dur_s is not None:
            durations_s_by_id[cid] = dur_s
        if dist_m is not None:
            distances_m_by_id[cid] = dist_m

    # D) Ranking (ETA-first) & cut ≤ limit
    items: List[PlaceScored] = rank_and_cut(
        seeds,
        durations_s_by_id=durations_s_by_id,
        distances_m_by_id=distances_m_by_id,
        mode=mode,
        weights=weights,
        prior=prior,
        limit=limit,
    )

    return {
        "reason_code": reason,
        "need_confirm": False,
        "origin": origin,
        "items": items,
        "candidates": None,
    }
