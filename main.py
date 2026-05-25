from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import users, settings, deals
from scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield


app = FastAPI(title="Amazon Harvest App", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(deals.router, prefix="/api/deals", tags=["deals"])


@app.get("/")
def root():
    return {"status": "ok", "app": "Amazon Harvest"}
