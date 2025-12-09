from typing import List, Tuple, Literal, Optional, Annotated
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.routing import route_preview, GeoPoint

router = APIRouter(prefix="/route", tags=["route"])

_ALIAS = {
    "car": "driving",
    "moto": "motorcycling",
    "walk": "walking",
    "truck": "truck",
    # nếu có "truck" ở provider thì map thêm "truck": "truck"
}

class Point(BaseModel):
    lat: Annotated[float, Field(ge=-90, le=90)]
    lng: Annotated[float, Field(ge=-180, le=180)]

class RouteRequest(BaseModel):
    origin: Point
    dest: Point
    mode: str = Field(default="motorcycling",
                      description="driving|motorcycling|walking|truck")
    lang: str = "vi"


# ====== Response models ======
class RouteGeometry(BaseModel):
    coordinates: List[Tuple[float, float]] = Field(
        ..., description="List of [lat, lng] along the route polyline"
    )
    bounds: List[float] = Field(
        ...,
        min_items=4,
        max_items=4,
        description="[min_lat, min_lng, max_lat, max_lng]",
    )


class RouteSummary(BaseModel):
    eta_s: Optional[float] = Field(
        None, description="ETA in seconds (may be null if provider doesn’t return)"
    )
    distance_m: Optional[float] = Field(
        None, description="Distance in meters (may be null if provider doesn’t return)"
    )


class RouteResponse(BaseModel):
    geometry: RouteGeometry
    summary: RouteSummary


@router.post("/preview", response_model=RouteResponse, response_model_exclude_none=True)
async def preview(req: RouteRequest) -> RouteResponse:
    mode = _ALIAS.get(req.mode, req.mode)
    if mode not in {"driving", "motorcycling", "walking", "truck"}:
        raise HTTPException(status_code=422, detail="Invalid mode")

    origin = GeoPoint(lat=req.origin.lat, lng=req.origin.lng)
    dest = GeoPoint(lat=req.dest.lat, lng=req.dest.lng)

    data = await route_preview(origin=origin, dest=dest, mode=mode, lang=req.lang)
    # FastAPI sẽ tự convert dict trả về thành RouteResponse
    return data
