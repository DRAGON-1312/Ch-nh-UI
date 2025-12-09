from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.autocomplete import suggest_places

router = APIRouter(prefix="/autocomplete", tags=["autocomplete"])


# ========= Schemas =========
class AutocompleteItem(BaseModel):
    """Item trả về cho dropdown autocomplete trên FE."""

    place_id: str = Field(..., description="TrackAsia place_id (hoặc official_id)")
    main_text: str = Field(..., description="Dòng chính: tên địa điểm / số nhà + tên đường")
    secondary_text: str = Field(
        "", description="Dòng phụ: quận/huyện, tỉnh/thành, quốc gia..."
    )
    description: str = Field(
        "", description="Mô tả đầy đủ, có thể dùng làm tooltip hoặc debug"
    )

    lat: Optional[float] = Field(
        None, description="Latitude nếu provider trả (có thể None)"
    )
    lng: Optional[float] = Field(
        None, description="Longitude nếu provider trả (có thể None)"
    )

    # Extra thông tin hành chính (dùng khi cần)
    official_id: Optional[str] = Field(
        None, description="Mã hành chính chính thức (nếu có)"
    )
    old_description: Optional[str] = Field(
        None, description="Mô tả theo địa giới cũ (nếu có)"
    )
    old_formatted_address: Optional[str] = Field(
        None, description="Địa chỉ cũ dạng đầy đủ (nếu có)"
    )


# ========= Endpoints =========
@router.get(
    "/origin-suggest",
    response_model=List[AutocompleteItem],
    response_model_exclude_none=True,
)
async def origin_suggest(
    q: str = Query(
        ...,
        min_length=1,
        max_length=128,
        description="Chuỗi user đang gõ (ví dụ: '227 Nguyen Van Cu')",
    ),
    lat: Optional[float] = Query(
        None,
        ge=-90,
        le=90,
        description="Latitude của điểm bias (ví dụ: origin hiện tại)",
    ),
    lng: Optional[float] = Query(
        None,
        ge=-180,
        le=180,
        description="Longitude của điểm bias (ví dụ: origin hiện tại)",
    ),
    limit: int = Query(
        5,
        ge=1,
        le=10,
        description="Số lượng gợi ý tối đa",
    ),
    new_admin: bool = Query(
        True,
        description="Ưu tiên địa giới hành chính mới (new_admin=true)",
    ),
    include_old_admin: bool = Query(
        False,
        description="Trả thêm địa chỉ theo địa giới cũ (include_old_admin=true)",
    ),
) -> List[AutocompleteItem]:
    """
    Gợi ý địa điểm / địa chỉ cho ô Origin (autocomplete v2 của TrackAsia).

    FE dùng:
        GET /autocomplete/origin-suggest?q=...&lat=...&lng=...

    - Nếu có lat/lng → provider sẽ bias kết quả quanh khu vực đó.
    - Nếu không truyền lat/lng → autocomplete global.
    """
    center = (lat, lng) if lat is not None and lng is not None else None

    suggestions = await suggest_places(
        query=q,
        center=center,
        limit=limit,
        new_admin=new_admin,
        include_old_admin=include_old_admin,
    )

    return [
        AutocompleteItem(
            place_id=s.place_id,
            main_text=s.main_text,
            secondary_text=s.secondary_text,
            description=s.description,
            lat=s.lat,
            lng=s.lng,
            official_id=s.official_id,
            old_description=s.old_description,
            old_formatted_address=s.old_formatted_address,
        )
        for s in suggestions
    ]
