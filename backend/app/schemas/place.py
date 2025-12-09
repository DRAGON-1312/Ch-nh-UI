from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .location import GeoPoint
from .common import Tag


class ProviderIds(BaseModel):
    """
    Id của cùng một địa điểm ở các provider khác nhau.

    - TrackAsia: dùng cho geocode / nearby / matrix / directions (routing, ETA).
    - SerpAPI (engine=google_maps): dùng cho POI search + rating + reviews.
    """

    trackasia_id: Optional[str] = Field(
        default=None,
        description="Id/feature_id của POI trong TrackAsia (nếu có).",
    )
    serpapi_place_id: Optional[str] = Field(
        default=None,
        description="data_id/place_id trong SerpAPI (Google Maps engine), nếu có.",
    )


class PlaceCandidate(BaseModel):
    """
    POI tối giản sau bước normalize, dùng làm input cho ranking.

    - Map từ kết quả TrackAsia / SerpAPI.
    - KHÔNG chứa budget/price/open_now/hours (đã bỏ hẳn trong project).
    """

    id: str = Field(
        ...,
        description=(
            "Internal id ổn định dùng trong pipeline "
            "(thường = serpapi_place_id hoặc trackasia_id, tuỳ provider chính)."
        ),
    )

    provider_ids: ProviderIds = Field(
        default_factory=ProviderIds,
        description="Id của cùng POI trong từng provider (TrackAsia, SerpAPI...).",
    )

    name: str = Field(..., min_length=1, description="Tên địa điểm hiển thị trên UI.")
    address: Optional[str] = Field(
        default=None,
        description="Địa chỉ hiển thị (formatted address).",
    )

    location: GeoPoint = Field(
        ...,
        description="Toạ độ dùng làm tâm cho search, matrix, route.",
    )

    tags: List[Tag] = Field(
        default_factory=list,
        description="Loại hình: restaurant / cafe / hotel... Thường chỉ 1 tag.",
    )

    # Tín hiệu phục vụ ranking – chỉ rating & tổng số lượt đánh giá.
    rating: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=5.0,
        description="Điểm đánh giá thang 0–5 (chuẩn hoá từ SerpAPI, nếu có).",
    )
    user_ratings_total: Optional[int] = Field(
        default=None,
        ge=0,
        description="Tổng số lượt đánh giá (reviews count), nếu có).",
    )

    # ----- Validators & helpers -----

    @field_validator("tags", mode="before")
    @classmethod
    def _norm_tags(cls, v):
        """
        Cho phép truyền tags dạng chuỗi hoặc list chuỗi → tự map sang Tag.
        Ví dụ:
            'restaurant'         -> [Tag.RESTAURANT]
            ['cafe', 'hotel']    -> [Tag.CAFE, Tag.HOTEL]
        """
        if isinstance(v, (list, tuple)):
            return list(Tag.normalize_many(v))
        if isinstance(v, str):
            return list(Tag.normalize_many([v]))
        return []

    def distance_km_to(self, point: GeoPoint) -> float:
        """Haversine distance (km) từ POI đến 1 GeoPoint bất kỳ."""
        return self.location.distance_km_to(point)


class PlaceEnriched(PlaceCandidate):
    """
    PlaceCandidate sau khi đã gắn thêm tín hiệu từ TrackAsia Matrix/Route
    và module ranking. Thường dùng cho tầng services / API trả ra FE.

    Lưu ý:
    - VẪN không có budget/price/open_now/hours.
    - 'mode' (driving / walking / ...) là tham số của request, không lưu ở đây.
    """

    distance_m: Optional[float] = Field(
        default=None,
        ge=0,
        description="Khoảng cách route ngắn nhất từ origin đến POI (m), lấy từ TrackAsia.",
    )
    duration_s: Optional[float] = Field(
        default=None,
        ge=0,
        description="ETA từ origin đến POI (giây), ưu tiên dùng trong ranking.",
    )
    score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Điểm tổng [0,1] sau khi áp dụng WeightConfig (ranking).",
    )
