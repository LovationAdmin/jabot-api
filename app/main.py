import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routes import auth, persons, tree, media

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Origines statiques toujours autorisées
_STATIC_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
]

# Regex: tous les sous-domaines *.vercel.app
_VERCEL_ORIGIN_RE = re.compile(r"https://[a-z0-9-]+\.vercel\.app$")


def _is_allowed(origin: str) -> bool:
    if origin in _STATIC_ORIGINS:
        return True
    if settings.FRONTEND_URL and origin == settings.FRONTEND_URL:
        return True
    return bool(_VERCEL_ORIGIN_RE.match(origin))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Démarrage de l'application JABOT API")
    yield
    logger.info("Arrêt de l'application JABOT API")


app = FastAPI(
    title="JABOT Genealogy API",
    description="API de généalogie pour le marché africain",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Contourné par allow_origin_regex ci-dessous
    allow_origin_regex=r"https://[a-z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api"

app.include_router(auth.router, prefix=f"{API_PREFIX}/auth", tags=["Authentification"])
app.include_router(persons.router, prefix=f"{API_PREFIX}/persons", tags=["Personnes"])
app.include_router(tree.router, prefix=f"{API_PREFIX}/tree", tags=["Arbre généalogique"])
app.include_router(media.router, prefix=f"{API_PREFIX}/media", tags=["Médias"])


@app.get("/", tags=["Health"])
async def root():
    return {"message": "JABOT Genealogy API", "version": "1.0.0", "status": "ok"}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.error(f"Erreur non gérée: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Une erreur interne s'est produite"},
    )
