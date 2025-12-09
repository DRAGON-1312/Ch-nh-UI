from __future__ import annotations

from enum import Enum
from typing import Optional, Iterable, Set, Dict, Any, Generic, TypeVar, List
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# Enums & constants

class ReasonCode(str, Enum):
    """Empty-state & lỗi chuẩn hoá cho toàn bộ API."""
    OK = "OK"
    BUSINESS_NO_MATCH = "BUSINESS_NO_MATCH"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    TECHNICAL = "TECHNICAL"


class DegradeFlag(str, Enum):
    """Các cờ 'giảm chất lượng' để FE hiển thị cảnh báo tinh tế."""
    POSSIBLE_STALE = "POSSIBLE_STALE"                  # cache cũ
    HITS_RATE_LIMIT = "HITS_RATE_LIMIT"                # bị throttle
    FALLBACK_RADIUS_EXPANDED = "FALLBACK_RADIUS_EXPANDED"
    LOW_CONFIDENCE_ORIGIN = "LOW_CONFIDENCE_ORIGIN"
    PARTIAL_MATCHED = "PARTIAL_MATCHED"


class Tag(str, Enum):
    """Loại đối tượng tìm kiếm (chuẩn hoá)."""
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    HOTEL = "hotel"

    @staticmethod
    def normalize_many(tags: Iterable[str]) -> Set["Tag"]:
        out: Set[Tag] = set()
        for t in tags or []:
            t_norm = (t or "").strip().lower()
            if t_norm in {"restaurant", "restaurants", "food"}:
                out.add(Tag.RESTAURANT)
            elif t_norm in {"cafe", "coffee"}:
                out.add(Tag.CAFE)
            elif t_norm in {"hotel", "lodging"}:
                out.add(Tag.HOTEL)
        return out


class Provider(str, Enum):
    """Chỉ provider dữ liệu địa điểm/địa lý (không gồm LLM)."""
    TRACKASIA = "trackasia"
    SERPAPI = "serpapi"  # Google Places qua SerpAPI


# =========================
# Value objects
# =========================

class GeoPoint(BaseModel):
    """
    Điểm địa lý hợp lệ, không cho phép (0,0) trong ngữ cảnh đã chuẩn hoá.
    Nếu cần biểu diễn 'thiếu toạ độ', hãy để ở model khác với confidence=0.
    """
    lat: float
    lng: float

    model_config = {
        "extra": "ignore",
        "frozen": True,  # immutable để an toàn khi cache/hash
    }

    @field_validator("lat")
    @classmethod
    def _lat_in_range(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("lat must be within [-90, 90]")
        return float(v)

    @field_validator("lng")
    @classmethod
    def _lng_in_range(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("lng must be within [-180, 180]")
        return float(v)

    @model_validator(mode="after")
    def _not_null_island(self) -> "GeoPoint":
        if abs(self.lat) < 1e-12 and abs(self.lng) < 1e-12:
            raise ValueError("lat/lng cannot both be 0.0 (null island)")
        return self

    def to_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lng)

    def to_geojson(self) -> Dict[str, Any]:
        return {"type": "Point", "coordinates": [self.lng, self.lat]}


# Error / Envelope

class ErrorModel(BaseModel):
    """
    Mẫu lỗi chuẩn để FE hiển thị nhất quán.
    - reason_code: dùng ReasonCode ở trên.
    - provider: nếu lỗi/quota đến từ provider cụ thể.
    - status: HTTP status nếu có (vd 429/403/5xx).
    """
    reason_code: ReasonCode = Field(default=ReasonCode.TECHNICAL)
    message: str = Field(default="Unexpected error")
    provider: Optional[Provider] = None
    status: Optional[int] = None
    debug_id: str = Field(default_factory=lambda: uuid4().hex)
    details: Optional[Dict[str, Any]] = None


T = TypeVar("T")

class APIResponse(BaseModel, Generic[T]):
    """
    Phong bì (envelope) response thống nhất cho routers.
    """
    reason_code: ReasonCode = ReasonCode.OK
    degrade_flags: List[str] = Field(default_factory=list)  # dùng Enum.value để dễ serialize
    data: Optional[T] = None
    debug: Dict[str, Any] = Field(default_factory=dict)


# Helpers

def pick_reason_code(
    *,
    provider_quota: bool = False,
    technical_error: bool = False,
    empty: bool = False,
) -> ReasonCode:
    """
    Chọn ReasonCode theo ưu tiên trong spec:
    PROVIDER_QUOTA > TECHNICAL > BUSINESS_NO_MATCH > OK
    """
    if provider_quota:
        return ReasonCode.PROVIDER_QUOTA
    if technical_error:
        return ReasonCode.TECHNICAL
    if empty:
        return ReasonCode.BUSINESS_NO_MATCH
    return ReasonCode.OK
