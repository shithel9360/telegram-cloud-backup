import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import datetime

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Shared state (to be populated by the daemon)
state = {
    "total_files": 0,
    "total_size": "0 B",
    "uptime": "00:00:00",
    "status": "Starting...",
    "start_time": datetime.datetime.now()
}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/stats")
async def get_stats():
    # Calculate uptime
    delta = datetime.datetime.now() - state["start_time"]
    state["uptime"] = str(delta).split(".")[0]
    return state

def start_server():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="error")
