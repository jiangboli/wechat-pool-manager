import os
import base64
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from database import init_pool, close_pool, ensure_schema
from routers import dashboard, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    from database import get_conn, put_conn
    conn = get_conn()
    try:
        ensure_schema(conn)
    finally:
        put_conn(conn)
    yield
    close_pool()


app = FastAPI(title="Analytics Service", lifespan=lifespan)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")

ANALYTICS_PASSWORD = os.environ.get("ANALYTICS_PASSWORD", "")


@app.get("/")
async def root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html",
                          headers={"Cache-Control": "no-cache"})
    return {"message": "Analytics Service running"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not ANALYTICS_PASSWORD:
        return await call_next(request)

    path = request.url.path
    if path in ("/", "/health") or path.startswith("/api/") or path.startswith("/static/"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return Response(status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="Analytics"'},
                          content="need auth")
        try:
            creds = base64.b64decode(auth[6:]).decode()
            _, pw = creds.split(":", 1)
        except Exception:
            return Response(status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="Analytics"'})
        if not secrets.compare_digest(pw, ANALYTICS_PASSWORD):
            return Response(status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="Analytics"'})
    return await call_next(request)
