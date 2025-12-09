from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .client import TrackAsiaClient

# Endpoints
_PATH_V2 = "api/v2/geocode/json"   # JSON: results[]
_PATH_V1 = "api/v1/reverse"        # GeoJSON: features[]


@dataclass
class ReverseCandidate:
    place_id: str
    full_address: str
    name: str
    lat: float
    lng: float
    raw: Optional[Dict[str, Any]] = None


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


# parsers 
def _parse_v2(data: Dict[str, Any], limit: int) -> List[ReverseCandidate]:
    results = (data or {}).get("results") or []
    out: List[ReverseCandidate] = []
    for it in results[: max(0, limit)]:
        geom = (it.get("geometry") or {}).get("location") or {}
        out.append(
            ReverseCandidate(
                place_id=(it.get("place_id") or it.get("official_id") or "")[:128],
                name=(it.get("name") or it.get("formatted_address") or "")[:256],
                full_address=(it.get("formatted_address") or it.get("name") or "")[:512],
                lat=_f(geom.get("lat")),
                lng=_f(geom.get("lng")),
                raw=it,
            )
        )
    return out


def _parse_v1(data: Dict[str, Any], size: int) -> List[ReverseCandidate]:
    feats = (data or {}).get("features") or []
    out: List[ReverseCandidate] = []
    for f in feats[: max(0, size)]:
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
        props = f.get("properties") or {}
        out.append(
            ReverseCandidate(
                place_id=str(props.get("id") or props.get("gid") or "")[:128],
                full_address=(props.get("label") or props.get("name") or "")[:512],
                name=(props.get("name") or props.get("label") or "")[:256],
                lat=_f(coords[1]),
                lng=_f(coords[0]),
                raw=f,
            )
        )
    return out


# public API 
async def reverse_geocode(
    lat: float,
    lng: float,
    *,
    language: str = "vi",
    new_admin: bool = True,
    # v1-only params
    size: int = 1,
    radius_km: Optional[float] = None,
    # v2 preferences
    prefer_v2: bool = True,
    limit: int = 5,
    client: Optional[TrackAsiaClient] = None,
) -> List[ReverseCandidate]:
    """
    TrackAsia reverse geocoding (an toàn, không raise):
      - Mặc định gọi v2 (/api/v2/geocode/json) → results[]
      - Nếu lỗi/không có kết quả thì fallback v1 (/api/v1/reverse) → features[]
    Luôn trả List[ReverseCandidate]; lỗi → [].
    """
    owns = client is None
    client = client or TrackAsiaClient()

    try:
        # --- v2 first (đồng schema với Search v2) ---
        if prefer_v2:
            params_v2: Dict[str, Any] = {
                "latlng": f"{lat},{lng}",
                "language": language,
                "new_admin": "true" if new_admin else "false",
            }
            data_v2 = await client.get_json(_PATH_V2, params=params_v2)
            if isinstance(data_v2, dict) and not data_v2.get("error"):
                out_v2 = _parse_v2(data_v2, limit=limit)
                if out_v2:
                    return out_v2  # đã có kết quả

        # --- fallback v1 (GeoJSON) ---
        params_v1: Dict[str, Any] = {
            "lang": language,
            "point.lat": lat,
            "point.lon": lng,
            "new_admin": "true" if new_admin else "false",
            "size": max(1, size),
        }
        if radius_km is not None:
            params_v1["boundary.circle.radius"] = radius_km

        data_v1 = await client.get_json(_PATH_V1, params=params_v1)
        if isinstance(data_v1, dict) and not data_v1.get("error"):
            return _parse_v1(data_v1, size=size)

        # lỗi/không có → trả rỗng
        return []
    finally:
        if owns:
            await client.aclose()


# ---------- quick manual test ----------
if __name__ == "__main__":
    async def _demo():
        import os
        async with TrackAsiaClient(
            base_url=os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com"),
            token=os.getenv("TRACKASIA_TOKEN") or "public_key",   # đổi sang key thật khi deploy
            http2=bool(int(os.getenv("TRACKASIA_HTTP2", "0"))),
            max_retries=1,
        ) as c:
            res = await reverse_geocode(10.875620, 106.799140, size=1, radius_km=0.05, client=c)
            print(f"Got {len(res)} result(s).")
            for i, v in enumerate(res, 1):
                print(i, v.full_address, "@", f"{v.lat:.6f},{v.lng:.6f}")

    asyncio.run(_demo())
