import os
app = FastAPI()

@app.get("/")
async def root():
    return {"message": "test"}
