from __future__ import annotations
from typing import Any, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.common import ReasonCode, Tag
from app.schemas.location import NormalizedOrigin
from app.schemas.ranking import PlaceScored, WeightConfig, BayesianPrior
from app.services.ranked import search_ranked  # orchestrator

router = APIRouter(prefix="/search", tags=["ranked"])

# ---- Model nhỏ cho ứng viên ORIGIN (khi need_confirm = True)
class OriginCandidate(BaseModel):
    name: str
    lat: float
    lng: float
    display_address: Optional[str] = None
    place_id: Optional[str] = None
    types: Optional[Any] = None  # provider có thể trả None/str/list

class RankedRequest(BaseModel):
    # 1 trong 3 cách truyền origin
    origin_text: Optional[str] = None
    origin: Optional[NormalizedOrigin] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)

    # options
    tags: List[Tag] = Field(default_factory=list)
    radius_m: int = Field(default=2000, ge=100, le=15000)
    mode: str = Field(default="driving")
    seed_cap: int = Field(default=30, ge=5, le=60)
    limit: int = Field(default=20, ge=1, le=50)

    # i18n
    lang: str = "vi"
    country: str = "vn"

    # ranking params (tùy chọn)
    weights: Optional[WeightConfig] = None
    prior: Optional[BayesianPrior] = None

class RankedResponse(BaseModel):
    reason_code: ReasonCode
    need_confirm: bool
    origin: Optional[NormalizedOrigin] = None
    items: Optional[List[PlaceScored]] = None
    candidates: Optional[List[OriginCandidate]] = None  

@router.post("/ranked", response_model=RankedResponse)
async def post_ranked(req: RankedRequest) -> RankedResponse:
    if not (req.origin or req.origin_text or (req.lat is not None and req.lng is not None)):
        raise HTTPException(status_code=400,
                            detail="Provide at least one of: origin_text | origin | (lat & lng).")

    res = await search_ranked(
        origin_text=req.origin_text,
        origin=req.origin,
        lat=req.lat,
        lng=req.lng,
        tags=req.tags,
        radius_m=req.radius_m,
        mode=req.mode,
        seed_cap=req.seed_cap,
        limit=req.limit,
        lang=req.lang,
        country=req.country,
        weights=req.weights,
        prior=req.prior,
    )
    return RankedResponse(**res)
