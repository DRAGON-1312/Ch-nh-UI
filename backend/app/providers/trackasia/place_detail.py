from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .client import TrackAsiaClient

# Endpoint chuẩn: GET {BASE}/api/v2/place/details/json
_PATH_V2 = "api/v2/place/details/json"


# ---------- Models ----------
@dataclass
class PlaceDetail:
    """
    Thông tin chi tiết về 1 địa điểm từ TrackAsia.

    Đây là bản tối giản, đủ để:
    - Lấy lat/lng chuẩn từ place_id (dùng cho origin đã chọn từ Autocomplete).
    - Lấy địa chỉ chuẩn hóa (formatted_address / old_formatted_address).
    - (Tuỳ chọn) FE có thể dùng thêm name/vicinity/adr_address nếu muốn.
    """

    place_id: str
    lat: float
    lng: float

    # Thông tin hiển thị
    name: Optional[str] = None
    formatted_address: Optional[str] = None
    old_formatted_address: Optional[str] = None
    vicinity: Optional[str] = None
    adr_address: Optional[str] = None

    # Geometry gốc để debug / mở rộng sau này
    location_type: Optional[str] = None
    viewport: Optional[Dict[str, Any]] = None

    # Raw result để cần thì debug thêm
    raw: Optional[Dict[str, Any]] = None


# ---------- Helpers ----------
def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_result(result: Dict[str, Any]) -> Optional[PlaceDetail]:
    """
    Chuyển object `result` (từ JSON TrackAsia) → PlaceDetail.
    Trả None nếu thiếu lat/lng hoặc bị lỗi.
    """
    if not isinstance(result, dict):
        return None

    place_id = result.get("place_id") or ""
    name = result.get("name")

    formatted_address = result.get("formatted_address")
    # Nếu có hỗ trợ địa chỉ cũ
    old_formatted_address = result.get("old_formatted_address")
    vicinity = result.get("vicinity")
    adr_address = result.get("adr_address")

    geometry = result.get("geometry") or {}
    location = geometry.get("location") or {}

    lat = _to_float(location.get("lat"))
    lng = _to_float(location.get("lng"))
    if lat is None or lng is None:
        return None

    location_type = geometry.get("location_type")
    viewport = geometry.get("viewport")

    return PlaceDetail(
        place_id=place_id or "",
        lat=lat,
        lng=lng,
        name=name,
        formatted_address=formatted_address,
        old_formatted_address=old_formatted_address,
        vicinity=vicinity,
        adr_address=adr_address,
        location_type=location_type,
        viewport=viewport,
        raw=result,
    )


# ---------- Public API ----------
async def get_place_detail(
    place_id: str,
    *,
    language: str = "vi",
    new_admin: bool = True,
    include_old_admin: bool = True,
    client: Optional[TrackAsiaClient] = None,
) -> Optional[PlaceDetail]:
    """
    Gọi TrackAsia Place Detail v2 để lấy thông tin 1 địa điểm.

    - Không raise exception; nếu lỗi mạng / HTTP / JSON → trả None.
    - Luôn ưu tiên địa chỉ theo địa giới mới (new_admin=True).
    - Nếu include_old_admin=True và backend hỗ trợ, JSON có thể có:
        - old_formatted_address
        - old_address_components, ...
    """
    if not place_id:
        return None

    owns = client is None
    client = client or TrackAsiaClient()

    try:
        params: Dict[str, Any] = {
            "place_id": place_id,
            "language": language,
        }
        if new_admin:
            params["new_admin"] = "true"
            if include_old_admin:
                params["include_old_admin"] = "true"

        data = await client.get_json(_PATH_V2, params=params)

        # data kiểu: { status, result, error? }
        if not isinstance(data, dict):
            return None

        # TrackAsia convention: nếu có "error" thì coi như fail
        if data.get("error"):
            return None

        status = str(data.get("status") or "").upper()
        if status and status not in ("OK", "ZERO_RESULTS"):
            # status lạ → coi như lỗi
            return None

        result = data.get("result") or {}
        if not result:
            return None

        return _parse_result(result)

    except Exception:
        # Không để lỗi lan ra ngoài service
        return None
    finally:
        if owns:
            await client.aclose()


# ---------- Quick manual test ----------
if __name__ == "__main__":  # pragma: no cover
    async def _demo():
        import os

        sample_place_id = "17:venue:67addea7-6e98-5a17-8769-8399bbcea42f"  # ví dụ trong docs

        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",
            http2=bool(int(os.getenv("TRACKASIA_HTTP2", "0"))),
            max_retries=1,
        ) as c:
            detail = await get_place_detail(sample_place_id, client=c)
            print("DETAIL:", detail)

    asyncio.run(_demo())