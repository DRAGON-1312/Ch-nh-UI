from __future__ import annotations

from enum import Enum
from math import asin, cos, radians, sin, sqrt
from typing import Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator


# Low-level geo models
class GeoPoint(BaseModel):
    """
    Điểm địa lý đơn giản (lat, lng) dùng chung toàn project.
    - Validate phạm vi [-90, 90], [-180, 180]
    - Loại trừ (0,0) vì thường là sentinel “không có dữ liệu”.
    """

    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def _not_null_island(self) -> "GeoPoint":
        if self.lat == 0.0 and self.lng == 0.0:
            raise ValueError("lat/lng (0,0) không được dùng làm origin hợp lệ")
        return self

    def as_tuple(self) -> Tuple[float, float]:
        return (self.lat, self.lng)

    def distance_km_to(self, other: "GeoPoint") -> float:
        """
        Haversine distance ~km – dùng cho các filter đơn giản (cắt radius),
        không thay thế ETA từ TrackAsia Matrix.
        """
        R = 6371.0088
        lat1, lon1 = radians(self.lat), radians(self.lng)
        lat2, lon2 = radians(other.lat), radians(other.lng)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        return R * c


# Origin enums
class OriginKind(str, Enum):
    """
    NormalizedOrigin.kind – bản chất của origin sau khi intake.
    """
    POINT = "point"  # 1 điểm cụ thể (đa số case)
    AREA = "area"  # đơn vị hành chính (quận/phường/TP) -> dùng tâm
    POI = "poi"  # 1 POI cụ thể (chợ, landmark,...)
    UNKNOWN = "unknown"  # không phân loại được


class OriginSource(str, Enum):
    """
    NormalizedOrigin.source – nguồn input ban đầu của người dùng.
    Không gắn với provider (TrackAsia/SerpAPI), chỉ là UX source.
    """

    USER_TEXT = "user_text"  # user gõ địa chỉ/tên địa điểm
    MAP_CLICK = "map_click"  # user click trên bản đồ
    DEVICE_GEO = "device_geo"  # browser geolocation / GPS
    PLACE_ID = "place_id"  # user chọn từ suggestion (đã có id)
    SYSTEM_DEFAULT = "system_default"  # fallback mặc định (VD: TP.HCM)
    UNKNOWN = "unknown"


# Normalized origin
class NormalizedOrigin(BaseModel):
    """
    Mục tiêu Intake: mọi kiểu input (text, lat/lng, map click, suggestion)
    đều chuẩn hóa về NormalizedOrigin.

    - lat, lng: toạ độ cuối cùng dùng làm tâm cho search/matrix.
    - kind: POINT/AREA/POI...
    - place_id: id của origin nếu đến từ provider (TrackAsia), có thể None.
    - display_address: chuỗi dễ đọc để show trên UI (sidebar/header).
    - source: nguồn input ban đầu (USER_TEXT / MAP_CLICK / ...).
    - confidence: [0,1], Intake tự gán tuỳ độ chắc chắn (1.0 = chắc chắn).
    - raw_query: text user gõ ban đầu (optional, để debug/analytics).
    """

    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)

    kind: OriginKind = Field(default=OriginKind.POINT)
    place_id: Optional[str] = Field(
        default=None,
        description="Id từ provider (vd TrackAsia place_id), nếu có.",
    )
    display_address: Optional[str] = Field(
        default=None,
        description="Địa chỉ hiển thị đẹp cho UI (VD: '227 Nguyễn Văn Cừ, Q5, HCM').",
    )

    source: OriginSource = Field(default=OriginSource.UNKNOWN)
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Mức độ tin cậy (0..1). 1.0 = chắc chắn (map click, GPS).",
    )

    raw_query: Optional[str] = Field(
        default=None,
        description="Chuỗi user nhập ban đầu (nếu có).",
    )

    # ----- Validators -----

    @field_validator("lat", "lng", mode="before")
    @classmethod
    def _not_nan(cls, v: float) -> float:
        if v is None:
            raise ValueError("lat/lng không được None")
        return float(v)

    @model_validator(mode="after")
    def _not_null_island(self) -> "NormalizedOrigin":
        if self.lat == 0.0 and self.lng == 0.0:
            raise ValueError("lat/lng (0,0) không được dùng làm origin hợp lệ")
        return self

    # Convenience helpers
    @property
    def point(self) -> GeoPoint:
        """Convenience: trả về GeoPoint tương ứng."""
        return GeoPoint(lat=self.lat, lng=self.lng)

    def as_tuple(self) -> Tuple[float, float]:
        return (self.lat, self.lng)

    def distance_km_to(self, other: "NormalizedOrigin") -> float:
        return self.point.distance_km_to(other.point)

    # Convenience constructors 
    @classmethod
    def from_point(
        cls,
        lat: float,
        lng: float,
        *,
        source: OriginSource = OriginSource.MAP_CLICK,
        display_address: Optional[str] = None,
        confidence: float = 1.0,
        place_id: Optional[str] = None,
        raw_query: Optional[str] = None,
        kind: OriginKind = OriginKind.POINT,
    ) -> "NormalizedOrigin":
        """
        Helper cho case đã có lat/lng (map click, geolocation, provider trả về).
        """
        return cls(
            lat=lat,
            lng=lng,
            kind=kind,
            place_id=place_id,
            display_address=display_address,
            source=source,
            confidence=confidence,
            raw_query=raw_query,
        )

    @classmethod
    def from_text_geocode(
        cls,
        *,
        lat: float,
        lng: float,
        display_address: str,
        raw_query: str,
        place_id: Optional[str] = None,
        kind: OriginKind = OriginKind.POINT,
        confidence: float = 0.8,
    ) -> "NormalizedOrigin":
        """
        Helper cho flow: user gõ text → TrackAsia geocode → chọn 1 ứng viên.
        """
        return cls(
            lat=lat,
            lng=lng,
            kind=kind,
            place_id=place_id,
            display_address=display_address,
            source=OriginSource.USER_TEXT,
            confidence=confidence,
            raw_query=raw_query,
        )
