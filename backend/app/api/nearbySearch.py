from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.common import Tag, Provider, ReasonCode
from app.schemas.location import NormalizedOrigin, GeoPoint, OriginKind
from app.schemas.place import PlaceCandidate
from app.services.poi import nearby_search as svc_nearby
from app.services.intake_origin import intake_origin  # để resolve origin_text → NormalizedOrigin

router = APIRouter(prefix="/search", tags=["nearby"])


# ---------- Schemas ----------
class CandidateLite(BaseModel):
    name: str
    display_address: Optional[str] = None
    lat: float
    lng: float
    place_id: Optional[str] = None
    types: Optional[List[str]] = None

class NearbyUnifiedRequest(BaseModel):
    # Cách đưa origin (chọn một):
    origin_text: Optional[str] = Field(default=None, description="Địa chỉ/thành phố người dùng gõ")
    origin: Optional[NormalizedOrigin] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)

    # Tham số scoring cho Intake (tùy chọn – để bớt mơ hồ)
    bias_center: Optional[GeoPoint] = None
    radius_km: Optional[float] = Field(default=None, gt=0)
    alpha: float = Field(default=0.8, ge=0.5, le=0.95)

    # Nearby
    tags: List[Tag] = Field(default_factory=list, description="VD: ['restaurant', 'cafe']")
    radius_m: int = Field(default=2000, ge=100, le=15000)
    limit: int = Field(default=20, ge=1, le=50)

    # mode để dành cho bước Matrix sau (API giữ lại cho FE truyền thống)
    mode: str = Field(default="driving")

    # i18n / provider hints
    lang: str = Field(default="vi")
    country: str = Field(default="vn")

    # behavior Nearby
    enrich_missing_signals: bool = True
    seed_cap: int = Field(default=30, ge=5, le=60,
                          description="Số seed tối đa đẩy qua Matrix (Phase A)")
    sort_by_distance: bool = True

class NearbyUnifiedResponse(BaseModel):
    reason_code: ReasonCode = ReasonCode.OK
    need_confirm: bool = False
    origin: Optional[NormalizedOrigin] = None
    items: Optional[List[PlaceCandidate]] = None           # seed list (khi đã rõ)
    candidates: Optional[List[CandidateLite]] = None       # chỉ dùng khi need_confirm=True


# ---------- Endpoint: Intake → Nearby (no Ranking) ----------
@router.post("/nearby", response_model=NearbyUnifiedResponse)
async def post_nearby(req: NearbyUnifiedRequest) -> NearbyUnifiedResponse:
    """
    FE chỉ cần gửi origin_text | origin | (lat,lng) + radius_m + mode + tags.
    BE làm:
      1) Intake (nếu cần) → NormalizedOrigin (hoặc need_confirm + candidates)
      2) Nearby (seed_cap ~ 30) → trả items cho bước Matrix/Ranking xử lý tiếp.
    """
    # 1) Resolve origin
    origin = req.origin

    if origin is None and req.lat is not None and req.lng is not None:
        origin = NormalizedOrigin(
            lat=req.lat, lng=req.lng, kind=OriginKind.POINT,
            display_address=None, place_id=None
        )

    if origin is None and req.origin_text:
        origin_, reason, need_confirm, cands = await intake_origin(
            origin_text=req.origin_text,
            top_k=3,
            alpha=req.alpha,
            radius_km=req.radius_km,
            bias_center=req.bias_center,
        )
        if need_confirm:
            # trả về để FE hiển thị 2–3 ứng viên cho người dùng chọn
            c_lite = [
                CandidateLite(
                    name=c["name"],
                    display_address=c.get("display_address"),
                    lat=c["lat"],
                    lng=c["lng"],
                    place_id=c.get("place_id"),
                    types=c.get("types"),
                )
                for c in (cands or [])
            ]
            return NearbyUnifiedResponse(
                reason_code=reason,
                need_confirm=True,
                candidates=c_lite,
            )
        if origin_ is None:
            raise HTTPException(status_code=422, detail="Cannot resolve origin from origin_text.")
        origin = origin_

    if origin is None:
        raise HTTPException(status_code=400, detail="Provide one of: origin_text | origin | (lat,lng).")

    # 2) Nearby (seed_cap ~ 30), KHÔNG ranking ở đây
    seeds = await svc_nearby(
        origin=origin,
        tags=req.tags,
        radius_m=req.radius_m,
        limit=req.limit,                    # limit thực sự sẽ dùng ở bước Ranking sau
        lang=req.lang,
        country=req.country,
        provider=Provider.SERPAPI,
        enrich_missing_signals=req.enrich_missing_signals,
        seed_cap=req.seed_cap,
        sort_by_distance=req.sort_by_distance,
    )

    return NearbyUnifiedResponse(
        reason_code=ReasonCode.OK,
        need_confirm=False,
        origin=origin,
        items=seeds,
    )
