"""
Pessoa - Local VPN client management web UI
"""
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import re
import logging

from app import local_client

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Pessoa",
    description="Isolated VPN-tunneled browser sessions per client",
    version="0.3.0",
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def asset_v(path: str) -> str:
    try:
        return str(int((STATIC_DIR / path).stat().st_mtime))
    except FileNotFoundError:
        return "0"


templates.env.globals["asset_v"] = asset_v


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "title": "Pessoa - Dashboard"},
    )


@app.get("/api/clients")
async def list_clients(request: Request, format: str = "json"):
    clients = local_client.list_clients()

    if format == "html" or request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/client_cards.html",
            {"request": request, "clients": clients},
        )

    return JSONResponse(content={"clients": clients})


@app.post("/api/clients")
async def create_client(slug: str = Form(...)):
    if not re.match(r'^[a-z0-9-]+$', slug):
        raise HTTPException(status_code=400, detail="Invalid slug. Use only lowercase letters, numbers, and hyphens.")

    try:
        result = local_client.create_client(slug)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return JSONResponse(
        content={
            "message": f"Client '{slug}' created. Upload WireGuard config to complete setup.",
            "slug": slug,
            "state": "pending_config",
        },
        status_code=201,
    )


@app.get("/api/clients/{slug}")
async def get_client_status(slug: str):
    client = local_client.get_client(slug)
    if not client:
        raise HTTPException(status_code=404, detail=f"Client '{slug}' not found")
    return JSONResponse(content=client)


@app.delete("/api/clients/{slug}")
async def delete_client(slug: str):
    try:
        local_client.delete_client(slug)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return JSONResponse(content={"message": f"Client '{slug}' deleted"})


@app.post("/api/clients/{slug}/wireguard-config")
async def upload_wireguard_config(slug: str, config_file: UploadFile = File(...)):
    if not config_file.filename.endswith(".conf"):
        raise HTTPException(status_code=400, detail="File must be a .conf file")

    try:
        content = (await config_file.read()).decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read config: {e}")

    try:
        info = local_client.save_wireguard_config(slug, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return JSONResponse(content={
        "message": f"WireGuard config saved for '{slug}'",
        "slug": slug,
        "state": "ready",
        "vpn_endpoint": info.get("endpoint"),
    })


@app.post("/api/clients/{slug}/start")
async def start_client(request: Request, slug: str):
    try:
        await local_client.start_vpn(slug)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if request.headers.get("HX-Request"):
        clients = local_client.list_clients()
        return templates.TemplateResponse(
            "partials/client_cards.html",
            {"request": request, "clients": clients},
        )

    return JSONResponse(content={"message": f"VPN started for '{slug}'"})


@app.post("/api/clients/{slug}/stop")
async def stop_client(request: Request, slug: str):
    try:
        await local_client.stop_vpn(slug)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if request.headers.get("HX-Request"):
        clients = local_client.list_clients()
        return templates.TemplateResponse(
            "partials/client_cards.html",
            {"request": request, "clients": clients},
        )

    return JSONResponse(content={"message": f"VPN stopped for '{slug}'"})


@app.post("/api/clients/{slug}/browser/start")
async def start_browser(slug: str):
    try:
        pid = await local_client.launch_browser(slug)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(content={
        "message": f"Browser launched for '{slug}'",
        "pid": pid,
    })


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
