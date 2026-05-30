import logging
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Demarrage de l'application JABOT API")
    yield
    logger.info("Arret de l'application JABOT API")


app = FastAPI(
    title="JABOT Genealogy API",
    description="API de genealogie pour le marche africain",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS : on autorise le front configure (FRONTEND_URL) + le dev local, et via
# regex TOUTES les previews Vercel (chaque deploiement a un sous-domaine
# different : jabot-ui-xxxx.vercel.app), sinon chaque preview serait bloquee.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:3000", "http://localhost:5173"],
    allow_origin_regex=r"https://[a-z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api"

app.include_router(auth.router, prefix=f"{API_PREFIX}/auth", tags=["Authentification"])
app.include_router(persons.router, prefix=f"{API_PREFIX}/persons", tags=["Personnes"])
app.include_router(tree.router, prefix=f"{API_PREFIX}/tree", tags=["Arbre genealogique"])
app.include_router(media.router, prefix=f"{API_PREFIX}/media", tags=["Medias"])


@app.get("/", tags=["Health"])
async def root():
    return {"message": "JABOT Genealogy API", "version": "1.0.0", "status": "ok"}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.error(f"Erreur non geree: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Une erreur interne s'est produite"},
    )
