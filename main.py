import os

load_dotenv()

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "test"}
