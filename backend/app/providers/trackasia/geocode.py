# Mục đích: cung cấp một hàm bất đồng bộ forward_geocode 
# (chuyển addr text thô sang (lat, lng)) để gọi API của TrackAsia
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .client import TrackAsiaClient
from .place_detail import get_place_detail, PlaceDetail

# TrackAsia Places Search v2
_PATH = "api/v2/place/textsearch/json"


# Cấu trúc dữ liệu đại diện cho một kết quả địa điểm trả về từ API.
@dataclass
class ForwardCandidate:
    place_id: str
    name: str
    full_address: str
    lat: float
    lng: float
    raw: Optional[Dict[str, Any]] = None # Dữ liệu gốc trả về từ API (dạng dict), để lưu trữ thông tin chi tiết nếu cần.

# Chuyển đổi giá trị bất kỳ sang kiểu float
def _to_float(x: Any) -> Optional[float]:
    """
    Trả về float nếu parse được, ngược lại trả None.
    Không dùng 0.0 làm default để phân biệt rõ 'không có dữ liệu'.
    """
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


async def forward_geocode(
    query: str,
    *,
    language: str = "vi",
    new_admin: bool = True,  # trả về đơn vị hành chính mới
    include_old_admin: bool = False,  # ko trả về đơn vị hành chính cũ
    radius: Optional[int] = None,  # mét
    limit: int = 5,  # Số lượng kết quả tối đa.
    client: Optional[TrackAsiaClient] = None,  # Đối tượng TrackAsiaClient để tái sử dụng kết nối HTTP.
) -> List[ForwardCandidate]:
    """
    1. Gọi TrackAsia Places Text Search v2.
    2. Với mỗi result:
       - Lấy lat/lng từ geometry.location nếu có.
       - Nếu thiếu → fallback sang Place Detail v2 bằng place_id.
       - Nếu vẫn không có tọa độ → bỏ qua result.
    3. Trả về tối đa `limit` ForwardCandidate đã có tọa độ hợp lệ.
    """
    owns = client is None
    client = client or TrackAsiaClient()

    params: Dict[str, Any] = {
        "query": query,
        "language": language,
        "new_admin": "true" if new_admin else "false",
    }
    if include_old_admin and new_admin:
        params["include_old_admin"] = "true"
    if radius is not None:
        params["radius"] = int(radius)

    try:
        data = await client.get_json(_PATH, params=params)
        if not isinstance(data, dict) or data.get("error"):
            # có thể log `data.get("error")` ở tầng trên nếu cần
            return []

        results = data.get("results") or []
        out: List[ForwardCandidate] = []

        for it in results:
            if len(out) >= max(0, limit):
                break

            # ----- Lấy lat/lng từ textsearch -----
            geom = (it.get("geometry") or {}).get("location") or {}
            lat = _to_float(geom.get("lat"))
            lng = _to_float(geom.get("lng"))

            # ----- Nếu thiếu tọa độ → fallback sang Place Detail -----
            if (lat is None or lng is None) and (it.get("place_id") or it.get("official_id")):
                place_id = it.get("place_id") or it.get("official_id")
                detail: Optional[PlaceDetail] = await get_place_detail(
                    place_id=place_id,
                    language=language,
                    new_admin=new_admin,
                    include_old_admin=include_old_admin,
                    client=client,
                )
                if detail is not None:
                    lat, lng = detail.lat, detail.lng

            # Sau khi đã fallback mà vẫn không có tọa độ → bỏ qua result này
            if lat is None or lng is None or (lat == 0.0 and lng == 0.0):
                continue

            out.append(
                ForwardCandidate(
                    place_id=(it.get("place_id") or it.get("official_id") or "")[:128],
                    name=(it.get("name") or it.get("formatted_address") or "")[:256],
                    full_address=(it.get("formatted_address") or it.get("name") or "")[:512],
                    lat=lat,
                    lng=lng,
                    raw=it,
                )
            )

        return out
    finally:
        if owns:
            await client.aclose()


# Quick manual test (safe defaults, không treo)
if __name__ == "__main__":
    async def _demo():
        import os
        from .client import TrackAsiaClient
        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",  # đổi sang key thật nếu có
            http2=bool(int(os.getenv("TRACKASIA_HTTP2", "0"))),
            max_retries=1,
        ) as c:
            query = "Trường Đại học Khoa học Tự nhiên - ĐHQG TP.HCM - Cơ sở 1"
            # gọi thẳng client để xem lỗi/HTTP status
            params = {"query": query, "language": "vi", "new_admin": "true"}
            raw = await c.get_json("api/v2/place/textsearch/json", params=params)
            print("DEBUG:", {"error": raw.get("error"), "status": raw.get("status")})
            # dùng API chuẩn
            res = await forward_geocode(query, limit=2, client=c)
            print(f"Got {len(res)} result(s).")
            for i, v in enumerate(res, 1):
                print(i, v.name, f"{v.lat:.6f},{v.lng:.6f}", "|", v.full_address)

    asyncio.run(_demo())

