from typing import List, Optional, Union
from ipaddress import ip_address
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.acquisition.kite import ZerodhaMarketDataProvider
from app.acquisition.stage0 import Stage0Acquisition
from app.acquisition.stage1 import Stage1Acquisition
from app.acquisition.stage2 import Stage2Acquisition
from app.auth.zerodha import KiteAuthSession, KiteAuthenticationError
from app.config import settings
from app.security import safe_error

router = APIRouter()


class Stage2Request(BaseModel):
    symbols: Optional[Union[str, List[str]]] = None


def parse_symbols(value):
    if value is None:
        raise ValueError("symbols are required")
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, list):
        raise ValueError("symbols must be a comma-separated string or a list of strings")
    result = []
    for part in parts:
        symbol = str(part).strip().upper()
        if not symbol:
            raise ValueError("empty symbols are not allowed")
        if symbol in result:
            raise ValueError(f"duplicate symbol detected: {symbol}")
        result.append(symbol)
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _download(response: JSONResponse, stage: str) -> JSONResponse:
    import datetime
    suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S") + "Z"
    filename = f"nexus_{stage}_raw_{suffix}.json"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["X-Download-Filename"] = filename
    return response


def _response(payload: dict, stage: str) -> JSONResponse:
    response = _download(JSONResponse(content=jsonable_encoder(payload)), stage)
    execution_id = (payload.get("execution") or {}).get("execution_id")
    if execution_id:
        response.headers["X-Execution-ID"] = execution_id
    status = payload.get("status")
    if status is None and stage == "stage2":
        statuses = [item.get("status") for item in payload.get("symbols", [])]
        status = "SUCCESS" if "SUCCESS" in statuses else "ERROR"
    response.headers["X-Acquisition-Status"] = str(status or "ERROR")
    return response


def _provider(request: Request):
    return getattr(request.app.state, "kite_provider", None)


def _private_auth_client(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        address = ip_address(host)
    except ValueError:
        raise HTTPException(status_code=403, detail="Authentication is available only to local/private clients.") from None
    if not (address.is_loopback or address.is_private or address.is_link_local):
        raise HTTPException(status_code=403, detail="Authentication is available only to local/private clients.")


def _auth_session(request: Request) -> KiteAuthSession:
    session = getattr(request.app.state, "kite_auth", None)
    if session is None:
        session = KiteAuthSession(settings)
        request.app.state.kite_auth = session
    return session


@router.get("/mobile", response_class=HTMLResponse)
def mobile_ui():
    return HTMLResponse(content=MOBILE_HTML)


@router.post("/mobile/stage0/raw")
def mobile_stage0_raw(request: Request):
    try:
        payload = Stage0Acquisition(_provider(request)).acquire()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Stage 0 acquisition failed: {safe_error(exc)}") from exc
    return _response(payload, "stage0")


@router.post("/mobile/stage1/raw")
def mobile_stage1_raw(request: Request):
    try:
        payload = Stage1Acquisition().acquire()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Stage 1 acquisition failed: {safe_error(exc)}") from exc
    return _response(payload, "stage1")


@router.post("/mobile/stage2/raw")
def mobile_stage2_raw(body: Stage2Request, request: Request):
    try:
        symbols = parse_symbols(body.symbols if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        payload = Stage2Acquisition(_provider(request)).acquire(symbols)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Stage 2 acquisition failed: {safe_error(exc)}") from exc
    return _response(payload, "stage2")


@router.get("/auth/zerodha/login")
def auth_zerodha_login(request: Request, _: None = Depends(_private_auth_client)):
    try:
        return {"login_url": _auth_session(request).get_login_url()}
    except KiteAuthenticationError:
        raise HTTPException(status_code=503, detail="Kite login is not configured.") from None


@router.get("/api/callback")
def auth_callback(request: Request, action: Optional[str] = None, status: Optional[str] = None, request_token: Optional[str] = None, _: None = Depends(_private_auth_client)):
    if not request_token:
        raise HTTPException(status_code=400, detail="request_token is required")
    auth_session = _auth_session(request)
    try:
        result = auth_session.authenticate(request_token)
        provider = ZerodhaMarketDataProvider(auth_session.create_kite_client())
        provider.refresh_instruments()
        request.app.state.kite_provider = provider
        return RedirectResponse("/mobile?auth=success", status_code=303)
    except KiteAuthenticationError:
        raise HTTPException(status_code=401, detail="Kite authentication failed; start a new login.") from None
    except Exception:
        raise HTTPException(status_code=502, detail="Kite session was saved but market data initialization failed.") from None


@router.get("/auth/zerodha/status")
def auth_zerodha_status(request: Request, _: None = Depends(_private_auth_client)):
    return _auth_session(request).status()


@router.post("/auth/zerodha/logout")
def auth_zerodha_logout(request: Request, _: None = Depends(_private_auth_client)):
    _auth_session(request).clear()
    request.app.state.kite_provider = None
    return {"status": "logout", "authenticated": False}


MOBILE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nexus Mobile</title><style>
body{font-family:system-ui,sans-serif;max-width:680px;margin:0 auto;padding:16px;background:#f4f6f8;color:#18212b}.panel{background:#fff;padding:16px;margin:12px 0;border:1px solid #d7dde3;border-radius:8px}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}button,input{font:inherit;padding:10px;border:1px solid #8794a1;border-radius:6px}button{background:#174d67;color:white;cursor:pointer}input{flex:1;min-width:180px}.status{min-height:24px;margin-top:8px}</style></head>
<body><h1>Nexus Mobile</h1><section class="panel"><div class="row"><button id="kite-login">CONNECT ZERODHA</button><button id="kite-logout">DISCONNECT</button></div><div id="kite-status" class="status">Checking Kite session...</div></section><section class="panel"><div class="row"><button data-stage="stage0">RUN STAGE 0</button><button data-stage="stage1">RUN STAGE 1</button></div><div id="base-status" class="status">Ready</div></section><section class="panel"><label for="symbols">Stage 2 symbols</label><div class="row"><input id="symbols" value="HDFCBANK, COALINDIA, BSE"><button id="stage2">GET STAGE 2 DATA</button></div><div id="stage2-status" class="status">Ready</div></section><script>
async function acquire(path,payload,status){const el=document.getElementById(status);el.textContent='Acquiring...';try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload||{})});const text=await r.text();if(!r.ok)throw new Error(text);const data=JSON.parse(text);const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download=r.headers.get('X-Download-Filename')||'nexus-mobile.json';a.click();URL.revokeObjectURL(a.href);el.textContent='Complete';}catch(e){el.textContent='Failed: '+e.message;}}
async function refreshKiteStatus(){try{const r=await fetch('/auth/zerodha/status');const state=await r.json();document.getElementById('kite-status').textContent=state.authenticated?'Connected':'Login required';}catch(e){document.getElementById('kite-status').textContent='Kite status unavailable';}}
document.getElementById('kite-login').addEventListener('click',async()=>{try{const r=await fetch('/auth/zerodha/login');const data=await r.json();if(!r.ok)throw new Error();window.location.assign(data.login_url);}catch(e){document.getElementById('kite-status').textContent='Unable to start Kite login';}});
document.getElementById('kite-logout').addEventListener('click',async()=>{await fetch('/auth/zerodha/logout',{method:'POST'});await refreshKiteStatus();});
document.querySelectorAll('[data-stage]').forEach(b=>b.addEventListener('click',()=>acquire('/mobile/'+b.dataset.stage+'/raw',{},'base-status')));document.getElementById('stage2').addEventListener('click',()=>acquire('/mobile/stage2/raw',{symbols:document.getElementById('symbols').value},'stage2-status'));refreshKiteStatus();
</script></body></html>
"""
