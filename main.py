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
import logging
import requests
from bs4 import BeautifulSoup

load_dotenv()

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "test"}
