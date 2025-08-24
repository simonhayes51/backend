import os
import json
import asyncpg
import aiohttp
import csv
import io
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional
import logging
import requests
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi import Query

load_dotenv()
app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Railway deployment test"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
