from fastapi import FastAPI

from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

from app.db import Base, engine

from app.routers import auth, jobs, users, vrs_profiles, gnss_stations

from app.routers import vrs_advanced
from app.routers import user_advanced_credentials

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="API do OrbitBase para autenticação, usuários, jobs VRS e perfis de processamento.",
)

cors_origins = [
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:5501",
    "http://localhost:5501",
]

if hasattr(settings, "cors_origin_list") and settings.cors_origin_list:

    if isinstance(settings.cors_origin_list, list):

        cors_origins.extend(settings.cors_origin_list)

# remove duplicadas preservando ordem

cors_origins = list(dict.fromkeys(cors_origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Requested-With",
        "ngrok-skip-browser-warning",
    ],
    expose_headers=[
        "Authorization",
        "Content-Type",
        "Content-Disposition",
    ],
    max_age=600,
)

app.include_router(auth.router)

app.include_router(users.router)

app.include_router(jobs.router)

app.include_router(vrs_profiles.router)

app.include_router(gnss_stations.router)

app.include_router(vrs_advanced.router)
app.include_router(user_advanced_credentials.router)


@app.get("/")
def root():

    return {
        "name": settings.app_name,
        "env": settings.app_env,
        "status": "ok",
    }


@app.get("/health")
def health():

    return {"status": "healthy"}
