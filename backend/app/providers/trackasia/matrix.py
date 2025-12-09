# Tính thời gian di chuyển nhanh nhất giữa các cặp tọa độ. Trả về thời gian hoặc khoảng cách hoặc cả hai giữa các cặp tọa độ.
# Link: https://docs.track-asia.com/vi/api-integration/distance-matrix/v1/
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import TrackAsiaClient


# ---------- Models ----------
@dataclass
class ODResult:
    """Kết quả 1 → dst_index (ETA & distance). None nếu provider không trả về."""
    dst_index: int
    duration_s: Optional[float]   # giây
    distance_m: Optional[float]   # mét
    raw: Optional[Dict[str, Any]] = None
    
     # convenience units
    @property
    def duration_h(self) -> Optional[float]:
        """Đổi giây → giờ, làm tròn nhẹ cho UI"""
        return round(self.duration_s / 3600.0, 3) if self.duration_s is not None else None

    @property
    def distance_km(self) -> Optional[float]:
        """Đổi mét → km, làm tròn nhẹ cho UI"""
        return round(self.distance_m / 1000.0, 3) if self.distance_m is not None else None


# ---------- Helpers ----------
def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _parse_dm_v1(data: Dict[str, Any], n: int) -> List[ODResult]:
    """
    Parse theo docs: có thể trả durations[][], distances[][]
    """
    out: List[ODResult] = []
    if not isinstance(data, dict) or data.get("error"):
        return out

    # OSRM-like: durations/distances là 2D (sources x destinations)
    durations = data.get("durations")
    distances = data.get("distances")

    if isinstance(durations, list) or isinstance(distances, list):
        row_t = durations[0] if isinstance(durations, list) and durations else []
        row_d = distances[0] if isinstance(distances, list) and distances else []
        for i in range(n):
            dur = _f(row_t[i]) if i < len(row_t) else None
            dis = _f(row_d[i]) if i < len(row_d) else None
            out.append(ODResult(i, dur, dis))
        return out

    # Một vài cụm có thể bọc 'data'
    payload = data.get("data")
    if isinstance(payload, dict):
        return _parse_dm_v1(payload, n)

    return out


# ---------- Public API ----------
async def distance_matrix_v1(
    profile: str,
    coordinates_lnglat: str,
    *,
    annotations: str = "duration",
    sources: Optional[str] = None,
    destinations: Optional[str] = None,
    fallback_speed: Optional[float] = None,
    client: Optional[TrackAsiaClient] = None,
) -> Dict[str, Any]:
    """
    Gọi thẳng Distance Matrix v1.
    profile: car | moto | truck | walk
    coordinates_lnglat: 'lng,lat;lng,lat;...'
    """
    owns = client is None
    client = client or TrackAsiaClient()
    path = f"distance-matrix/v1/{profile}/{coordinates_lnglat}"

    params: Dict[str, Any] = {"annotations": annotations}
    if sources:
        params["sources"] = sources
    if destinations:
        params["destinations"] = destinations
    if fallback_speed is not None:
        params["fallback_speed"] = fallback_speed

    try:
        return await client.get_json(path, params=params)
    finally:
        if owns:
            await client.aclose()


async def matrix_1_to_many(
    origin_lat: float,
    origin_lng: float,
    destinations: Sequence[Tuple[float, float]],
    *,
    profile: str = "car",                # car | moto | truck | walk
    annotations: str = "duration",       # duration | distance | duration,distance
    fallback_speed: Optional[float] = None,
    client: Optional[TrackAsiaClient] = None,
) -> List[ODResult]:
    """
    Tính ma trận 1→N đúng theo Distance Matrix v1.
    Trả list ODResult (độ dài = N). Không raise lỗi HTTP.
    """
    owns = client is None
    client = client or TrackAsiaClient()

    try:
        n = len(destinations)
        if n == 0:
            return []

        # COORDINATES phải là "lng,lat;lng,lat;..."
        parts = [f"{origin_lng},{origin_lat}"] + [f"{lng},{lat}" for (lat, lng) in destinations]
        coordinates = ";".join(parts)

        # sources: luôn 0 (origin). destinations: 1;2;...;N
        src = "0"
        dst = ";".join(str(i + 1) for i in range(n))

        data = await distance_matrix_v1(
            profile=profile,
            coordinates_lnglat=coordinates,
            annotations=annotations,
            sources=src,
            destinations=dst,
            fallback_speed=fallback_speed,
            client=client,
        )
        parsed = _parse_dm_v1(data, n)

        # Đảm bảo đủ N phần tử theo index
        out = [None] * n  # type: ignore
        for it in parsed:
            if 0 <= it.dst_index < n:
                out[it.dst_index] = it
        for i in range(n):
            if out[i] is None:  # type: ignore
                out[i] = ODResult(i, None, None, None)  # type: ignore
        return out  # type: ignore
    finally:
        if owns:
            await client.aclose()


def _to_profile(mode: str) -> str:
    m = (mode or "driving").lower()
    if m in ("driving", "car"):
        return "car"
    if m in ("motorcycling", "moto", "motorcycle", "scooter"):
        return "moto"
    if m in ("walking", "foot"):
        return "walk"
    if m in ("truck"):
        return "truck"
    return "car"


async def matrix_one_to_one(
    *,
    origin: tuple[float, float],
    dest: tuple[float, float],
    mode: str = "driving",
    annotations: str = "duration,distance",
    client: Optional[TrackAsiaClient] = None,
) -> tuple[Optional[float], Optional[float]]:
    """
    Trả về (eta_seconds, distance_meters) theo Distance Matrix v1.
    Không dùng asyncio.run hay client riêng → không đụng event loop của FastAPI.
    """
    o_lat, o_lng = origin
    d_lat, d_lng = dest

    profile = _to_profile(mode)
    owns = client is None
    client = client or TrackAsiaClient()

    try:
        # Tái sử dụng đúng hàm 1→N, với N=1
        results = await matrix_1_to_many(
            origin_lat=o_lat,
            origin_lng=o_lng,
            destinations=[(d_lat, d_lng)],
            profile=profile,
            annotations=annotations,
            client=client,
        )
        r = results[0] if results else None
        return (r.duration_s if r else None, r.distance_m if r else None)
    finally:
        if owns:
            await client.aclose()

# ---------- Quick manual test ----------
if __name__ == "__main__":
    async def _demo():
        import os
        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",  # thay bằng key thật khi deploy
        ) as c:
            origin = (10.819911, 106.606860)
            dests = [(10.875620, 106.799140)]
            res = await matrix_1_to_many(origin[0], origin[1], dests, profile="car", annotations="duration,distance", client=c)
            print("N:", len(res))
            for r in res:
                print(r.dst_index, 
                      "dur(s)=", r.duration_s, "s  (~", r.duration_h, "h)",
                      "dist(m)=", r.distance_m, "m  (~", r.distance_km, "km)",
                      )

    asyncio.run(_demo())
