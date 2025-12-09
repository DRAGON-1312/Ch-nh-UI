from __future__ import annotations
from typing import List, Tuple, Optional, Dict, Any
from fastapi import HTTPException
# KHÔNG dùng asyncio.run nữa
from app.providers.trackasia.directions import get_route_path_v2
from app.providers.trackasia.matrix import matrix_one_to_one

class GeoPoint:
    def __init__(self, lat: float, lng: float):
        self.lat = float(lat)
        self.lng = float(lng)

def _bounds_latlng(coords: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    lats = [p[0] for p in coords]
    lngs = [p[1] for p in coords]
    return min(lats), min(lngs), max(lats), max(lngs)

async def route_preview(origin: GeoPoint, dest: GeoPoint, *, mode: str = "driving", lang: str = "vi") -> Dict[str, Any]:
    try:
        paths = await get_route_path_v2(
            origin_lat=origin.lat, origin_lng=origin.lng,
            dest_lat=dest.lat,   dest_lng=dest.lng,
            mode=mode, language=lang,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Routing provider error: {e}")

    if not paths:
        raise HTTPException(status_code=502, detail="Routing provider error: no route returned")

    coords: Optional[List[Tuple[float, float]]] = None
    for p in paths:
        if getattr(p, "coords", None):
            coords = p.coords
            break
        if getattr(p, "polyline", None):
            try:
                import polyline as _poly
                coords = _poly.decode(p.polyline)
                break
            except Exception:
                pass

    if not coords:
        raise HTTPException(status_code=502, detail="Routing provider error: Empty coordinates")

    bmin_lat, bmin_lng, bmax_lat, bmax_lng = _bounds_latlng(coords)

    # ✅ PHẢI await vì matrix_one_to_one là async
    eta_s, dist_m = await matrix_one_to_one(
        origin=(origin.lat, origin.lng),
        dest=(dest.lat, dest.lng),
        mode=mode,
        annotations="duration,distance",
    )

    return {
        "geometry": {
            "coordinates": coords,
            "bounds": [bmin_lat, bmin_lng, bmax_lat, bmax_lng],
        },
        "summary": {
            "eta_s": eta_s,
            "distance_m": dist_m,
        },
    }
