"""Tiny downstream dependency the app calls during /work."""

import time

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    time.sleep(0.05)
    return {"ok": True}
