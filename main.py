import os
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Railway deployment test"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
