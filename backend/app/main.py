from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import intake as intake_router
from app.api import nearbySearch as nearby_router
from app.api import ranked
from app.api import routing as routing_api
from app.api import autocomplete as autocomplete_router
from app.api import chatbot as chatbot_router

app = FastAPI(
    title="NestFeast",
    version="1.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

print("🚀 Server running at: http://127.0.0.1:8000")
print("📘 Swagger UI: http://127.0.0.1:8000/docs")
print("📙 ReDoc: http://127.0.0.1:8000/redoc")
print("❤️ Health check: http://127.0.0.1:8000/healthz")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(intake_router.router)
app.include_router(nearby_router.router)
app.include_router(ranked.router)
app.include_router(routing_api.router)
app.include_router(autocomplete_router.router)
app.include_router(chatbot_router.router)

@app.get("/healthz")
def healthz():
    return {"ok": True}