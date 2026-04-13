from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.storage.db import init_db
from backend.storage.seed import seed_data
from backend.api.routes_services import router as services_router
from backend.api.routes_incidents import router as incidents_router
from backend.api.routes_chat import router as chat_router
from backend.api.routes_settings import router as settings_router

app = FastAPI(title="SRE Agent Demo")

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"


@app.on_event("startup")
def startup():
    init_db()
    seed_data()


@app.get("/")
def root():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

app.include_router(services_router)
app.include_router(incidents_router)
app.include_router(chat_router)
app.include_router(settings_router)
