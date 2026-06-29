from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import router

app = FastAPI(title="Upwork Proposal Pipeline")


@app.on_event("startup")
async def startup():
    from app.db import init_db
    await init_db()


# Include all routes
app.include_router(router)

# Mount static files
import os
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
