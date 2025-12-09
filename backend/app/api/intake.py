from __future__ import annotations

from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.common import ReasonCode
from app.schemas.location import NormalizedOrigin, GeoPoint
from app.services.intake_origin import (
    intake_origin,
    OriginIntakeError,
)

router = APIRouter(prefix="/intake", tags=["intake"])


# ---------- Schemas for API ----------

class CandidateLite(BaseModel):
    name: str
    display_address: Optional[str] = None
    lat: float
    lng: float
    place_id: Optional[str] = None
    types: Optional[List[str]] = None


class OriginIntakeRequest(BaseModel):
    # Nguồn đầu vào
    origin_text: Optional[str] = Field(None, description="Địa chỉ/thành phố người dùng gõ.")
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    source_hint: Optional[str] = Field(default=None, description="device | map | browser")

    # Tham số scoring (chỉ áp dụng cho origin_text)
    top_k: int = Field(default=3, ge=2, le=5)
    alpha: float = Field(default=0.8, ge=0.5, le=0.95)
    radius_km: Optional[float] = Field(default=None, gt=0)
    bias_center: Optional[GeoPoint] = Field(default=None)


class OriginIntakeResponse(BaseModel):
    origin: Optional[NormalizedOrigin] = None
    reason_code: ReasonCode
    need_confirm: bool = False
    candidates: Optional[List[CandidateLite]] = None


# ---------- Endpoint ----------

@router.post("/origin", response_model=OriginIntakeResponse)
async def post_intake_origin(req: OriginIntakeRequest) -> OriginIntakeResponse:
    if not (req.origin_text or (req.lat is not None and req.lng is not None)):
        raise HTTPException(status_code=400, detail="Require origin_text or (lat,lng).")

    # Clamp radius theo spec (<= 7km) – chỉ dùng khi có bias_center
    radius_km = req.radius_km
    if radius_km is not None and radius_km > 7:
        radius_km = 7

    try:
        origin, reason, need_confirm, cands = await intake_origin(
            origin_text=req.origin_text,
            lat=req.lat,
            lng=req.lng,
            source_hint=req.source_hint,
            top_k=req.top_k,
            alpha=req.alpha,
            radius_km=radius_km,
            bias_center=req.bias_center,
        )
    except OriginIntakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Không để rơi 500 mù mờ
        raise HTTPException(status_code=502, detail=f"Origin intake failed: {e}")

    candidates = [CandidateLite(**c) for c in (cands or [])] or None
    return OriginIntakeResponse(
        origin=origin,
        reason_code=reason,
        need_confirm=need_confirm,
        candidates=candidates,
    )
