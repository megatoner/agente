import logging
from fastapi import FastAPI
from app.config import get_settings
from app.agents.router import router as agents_router
from app.memory.store import init_memory_db
from app.dependencies import engine

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="Servidor de Agentes IA para Odoo ERP",
    version="1.0.0"
)

@app.on_event("startup")
def startup():
    init_memory_db(engine)

@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}

app.include_router(agents_router)
