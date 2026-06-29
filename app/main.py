import logging
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Upwork Proposal Pipeline")

# Ensure static dir exists
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.on_event("startup")
async def on_startup():
    from app.db import init_db
    await init_db()

# Include all routes
app.include_router(router)