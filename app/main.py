import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.middleware.rate_limit import RateLimitMiddleware
from app.routes import auth, persons, tree, media, audit, admin, invitations, ws, trees
from app.services.ws_manager import manager as ws_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Demarrage de l'application JABOT API")
    try:
        from app.database import engine
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        logger.info("Base de donnees accessible")
    except Exception as exc:
        logger.error(f"Echec de connexion a la base de donnees au demarrage: {exc}")
    await ws_manager.startup()
    yield
    await ws_manager.shutdown()
    logger.info("Arret de l'application JABOT API")


app = FastAPI(
    title="JABOT Genealogy API",
    description="API de genealogie pour le marche africain",
    version="1.0.0",
    lifespan=lifespan,
)

# Rate-limiting par IP (Redis). Ajouté EN PREMIER pour qu'il soit le plus
# interne : Starlette exécute le dernier middleware ajouté en premier, donc le
# CORS (ajouté juste après) enveloppe la réponse 429 et ajoute ses en-têtes —
# sinon le navigateur masquerait l'erreur derrière un blocage CORS.
app.add_middleware(RateLimitMiddleware)

# CORS : on autorise le front configure (FRONTEND_URL) + le dev local, et via
# regex le domaine custom jabotai.com (apex + tout sous-domaine : www, etc.)
# ainsi que TOUTES les previews Vercel (chaque deploiement a un sous-domaine
# different : jabot-ui-xxxx.vercel.app), sinon chaque preview serait bloquee.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:3000", "http://localhost:5173"],
    allow_origin_regex=r"https://([a-z0-9-]+\.)?(jabotai\.com|vercel\.app)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api"

app.include_router(auth.router, prefix=f"{API_PREFIX}/auth", tags=["Authentification"])
app.include_router(persons.router, prefix=f"{API_PREFIX}/persons", tags=["Personnes"])
app.include_router(tree.router, prefix=f"{API_PREFIX}/tree", tags=["Arbre genealogique"])
app.include_router(trees.router, prefix=f"{API_PREFIX}/trees", tags=["Arbres (multi-tenant)"])
app.include_router(media.router, prefix=f"{API_PREFIX}/media", tags=["Medias"])
app.include_router(audit.router, prefix=API_PREFIX, tags=["Audit"])
app.include_router(admin.router, prefix=f"{API_PREFIX}/admin", tags=["Admin"])
app.include_router(invitations.router, prefix=f"{API_PREFIX}/invitations", tags=["Invitations"])
app.include_router(ws.router, prefix="/ws", tags=["WebSocket"])


# GET + HEAD : certaines sondes (Render port-scan, uptime checks) interrogent la
# racine en HEAD ; sans cela elles reçoivent un 405 qui peut être interprété
# comme un échec de health check et déclencher des redémarrages intempestifs.
@app.api_route("/", methods=["GET", "HEAD"], tags=["Health"])
async def root():
    return {"message": "JABOT Genealogy API", "version": "1.0.0", "status": "ok"}


@app.api_route("/health", methods=["GET", "HEAD"], tags=["Health"])
async def health_check():
    return {"status": "healthy"}


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.error(f"Erreur non geree: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Une erreur interne s'est produite"},
    )
