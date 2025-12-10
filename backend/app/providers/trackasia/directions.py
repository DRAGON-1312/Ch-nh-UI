# Tìm đường đi từ điểm A đến điểm B.
# Link: https://docs.track-asia.com/vi/api-integration/directions/v2/
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .client import TrackAsiaClient

# Chuẩn docs: GET {BASE}/route/v2/directions/json
_PATH_V2 = "route/v2/directions/json"


@dataclass
class RoutePath:
    """Đường đi A→B (v2 không cam kết ETA/distance)."""
    mode: str
    polyline: Optional[str] = None                   # nếu API trả encoded polyline
    coords: Optional[List[Tuple[float, float]]] = None  # nếu là GeoJSON LineString
    raw: Optional[Dict[str, Any]] = None


def _unwrap_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Một số cụm trả {data:{...}} hoặc {response:{...}} → gỡ lớp bọc."""
    if not isinstance(data, dict):
        return {}
    if "data" in data and isinstance(data["data"], dict):
        return data["data"]
    if "response" in data and isinstance(data["response"], dict):
        return data["response"]
    return data


def _find_coords(obj) -> Optional[List[tuple[float, float]]]:
    # Tìm mảng GeoJSON coordinates [[lng,lat], ...] ở mọi tầng
    if isinstance(obj, dict):
        if "coordinates" in obj and isinstance(obj["coordinates"], list):
            coords = obj["coordinates"]
            if coords and isinstance(coords[0], (list, tuple)) and len(coords[0]) >= 2:
                return [(float(p[1]), float(p[0])) for p in coords]
        for v in obj.values():
            res = _find_coords(v)
            if res: return res
    elif isinstance(obj, list):
        for v in obj:
            res = _find_coords(v)
            if res: return res
    return None


def _find_polyline(obj) -> Optional[str]:
    # Tìm chuỗi polyline ở các khóa phổ biến (kể cả overview_polyline: {points: "..."} )
    if isinstance(obj, dict):
        # overview_polyline có thể là dict
        ov = obj.get("overview_polyline") or obj.get("overviewPolyline")
        if isinstance(ov, dict) and isinstance(ov.get("points"), str) and len(ov["points"]) >= 8:
            return ov["points"]
        for k in ("polyline", "encoded_polyline", "points"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) >= 8:
                return v
        for v in obj.values():
            res = _find_polyline(v)
            if res: return res
    elif isinstance(obj, list):
        for v in obj:
            res = _find_polyline(v)
            if res: return res
    return None


def _decode_polyline(poly: str) -> List[Tuple[float, float]]:
    """Giải mã Google-style encoded polyline → [(lat, lng), ...]."""
    if not isinstance(poly, str) or not poly:
        return []

    coords: List[Tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0
    length = len(poly)

    while index < length:
        # decode latitude
        shift = 0
        result = 0
        while True:
            b = ord(poly[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # decode longitude
        shift = 0
        result = 0
        while True:
            b = ord(poly[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coords.append((lat / 1e5, lng / 1e5))

    return coords


def _parse_v2_geometry(data: Dict[str, Any], mode: str) -> List[RoutePath]:
    out: List[RoutePath] = []
    if not isinstance(data, dict) or data.get("error"):
        return out

    d = _unwrap_payload(data)

    # 1) FeatureCollection
    feats = d.get("features")
    if isinstance(feats, list) and feats:
        for f in feats:
            geom = (f or {}).get("geometry") or {}
            if geom.get("type") == "LineString" and isinstance(geom.get("coordinates"), list):
                raw = geom["coordinates"]
                coords = [(float(p[1]), float(p[0])) for p in raw]
                out.append(RoutePath(mode=mode, coords=coords, raw=f))
        if out: return out

    # 2) results[]
    results = d.get("results")
    if isinstance(results, list) and results:
        for it in results:
            poly = _find_polyline(it)
            if poly:
                coords = _decode_polyline(poly)
            else:
                coords = _find_coords(it)
            out.append(RoutePath(mode=mode, polyline=poly, coords=coords, raw=it))
        if out:
            return out

    # 3) paths / routes (một số backend)
    for key in ("paths", "routes"):
        arr = d.get(key)
        if isinstance(arr, list) and arr:
            for it in arr:
                poly = _find_polyline(it)
                if poly:
                    coords = _decode_polyline(poly)
                else:
                    coords = _find_coords(it)
                out.append(RoutePath(mode=mode, polyline=poly, coords=coords, raw=it))
            if out:
                return out

    # 4) Last resort: quét toàn payload
    poly = _find_polyline(d)
    if poly:
        coords = _decode_polyline(poly)
    else:
        coords = _find_coords(d)
    if poly or coords:
        out.append(RoutePath(mode=mode, polyline=poly, coords=coords, raw=d))
    return out


async def get_route_path_v2(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    *,
    mode: str = "driving",      # docs: driving (khuyến nghị), motorcycling, walking, truck
    language: str = "vi",
    new_admin: bool = True,
    client: Optional[TrackAsiaClient] = None,
) -> List[RoutePath]:
    """
    Directions v2: trả danh sách RoutePath chứa geometry (polyline/coords).
    Không đọc ETA/distance (hãy dùng Distance Matrix v1 cho việc đó).
    """
    owns = client is None
    client = client or TrackAsiaClient()

    params: Dict[str, Any] = {
        "origin": f"{origin_lat},{origin_lng}",
        "destination": f"{dest_lat},{dest_lng}",
        "mode": mode,
        "language": language,
        "new_admin": "true" if new_admin else "false",
    }

    try:
        data = await client.get_json(_PATH_V2, params=params)
        return _parse_v2_geometry(data, mode=mode)
    finally:
        if owns:
            await client.aclose()


# Quick manual test
if __name__ == "__main__":
    async def _demo():
        import os
        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",  # đổi sang key thật khi deploy
        ) as c:
            # ví dụ trong docs: origin/destination theo lat,lng
            paths = await get_route_path_v2(10.819911, 106.606860, 10.875620, 106.799140, mode="motorcycling", client=c)
            # debug-short: nếu rỗng, in error/status để bạn thấy ngay lí do
            if not paths:
                raw = await c.get_json(_PATH_V2, {
                    "origin": "10.819911,106.606860",
                    "destination": "10.875620,106.799140",
                    "mode": "driving",
                    "language": "vi",
                    "new_admin": "true",
                })
                print("DEBUG:", {"error": raw.get("error"), "status": raw.get("status")})
            print("num paths:", len(paths))
            for i, p in enumerate(paths, 1):
                print(i, "poly?", bool(p.polyline), "coords?", bool(p.coords))
    asyncio.run(_demo())
