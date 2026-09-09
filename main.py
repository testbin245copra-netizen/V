from __future__ import annotations

import asyncio
import datetime
import logging
import os
import random
import sys
import threading as _threading
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import uvicorn
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse

import auto
import auto_async
from auto import CheckStatus

try:
    import psutil
    _MEMORY_CHECK = True
except ImportError:
    psutil = None
    _MEMORY_CHECK = False

# ══════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════
PORT             = int(os.environ.get("CHECKER_PORT", os.environ.get("PORT", "6767")))
REQUEST_TIMEOUT  = 90
MEMORY_LIMIT_PCT = 90

logging.basicConfig(level=logging.INFO, format="%(message)s",
                    handlers=[logging.StreamHandler()])
_log = logging.getLogger("main")

# ══════════════════════════════════════════════════════════════
#  Dead-site cache
# ══════════════════════════════════════════════════════════════
_PROXY_SIGNS = ("407", "CONNECT tunnel", "libcurl", "Proxy Authentication",
                "curl: (56)", "curl: (7)")

_SITE_TTL = {
    "returned 429":              600,
    "returned 503":              180,
    "returned 403":             1800,
    "returned 402":              300,
    "returned 422":              300,
    "returned 404":            86400,
    "could not extract session": 300,
    "curl: (28)":                 90,
    "Step 0 failed":              90,
}

_dead_sites: dict[str, float] = {}
_dead_lock   = _threading.Lock()
_mem_cache: dict = {"val": False, "ts": 0.0}


def _mark_dead(site_url: str, error_str: str) -> None:
    if not error_str or any(s in error_str for s in _PROXY_SIGNS):
        return
    for pattern, ttl in _SITE_TTL.items():
        if pattern in error_str:
            with _dead_lock:
                _dead_sites[site_url] = time.time() + ttl
            return


def _exc_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if exc.args and exc.args[0]:
        return str(exc.args[0])
    return str(exc) or ""


# ══════════════════════════════════════════════════════════════
#  Result normalization
# ══════════════════════════════════════════════════════════════
_APPROVED_KEYWORDS = (
    "3DS_AUTHENTICATION", "3DS_AUTH", "3DS",
    "AUTHENTICATION_REQUIRED", "ACTIONREQUIRED",
    "INSUFFICIENT_FUNDS", "INSUFFICIENT FUNDS", "NOT SUFFICIENT FUNDS",
    "INCORRECT_CVC", "INVALID_CVC", "SECURITY_CODE",
    "CVV", "CVC_MISMATCH",
)
_DECLINED_KEYWORDS = (
    "CARD_DECLINED", "DECLINED", "DO_NOT_HONOR", "GENERIC_ERROR",
    "EXPIRED_CARD", "PICKUP_CARD",
    "LOST_CARD", "STOLEN_CARD", "FRAUD", "CALL_ISSUER",
    "TRANSACTION_NOT_ALLOWED", "PROCESSING_ERROR",
    "PAYMENT_METHOD_NOT_AVAILABLE", "AUTHENTICATION_FAILED",
    "INVALID_NUMBER", "INCORRECT_NUMBER",
)
_INFRA_ERROR_KEYWORDS = (
    "STEP ", "FAILED:", "RETURNED 4", "RETURNED 5",
    "RETURNED 402", "RETURNED 422", "RETURNED 429",
    "CURL:", "CONNECT TUNNEL", "COULD NOT EXTRACT", "COULD NOT",
    "POLL ", "EXCEEDED 30", "PROXY", "TIMEOUT", "TIMED OUT",
    "INVENTORYRESERVATIONFAILURE", "NO SHOPIFY", "SESSION", "LIBCURL",
)


def normalize_result(status: str, result_str: str) -> tuple[str, str]:
    resp = (result_str or "").strip() or "UNKNOWN"
    up   = resp.upper()

    if any(k in up for k in ("ORDER_PLACED", "SUCCESSFULRECEIPT", "PROCESSEDRECEIPT")):
        return "charged", resp
    if any(k in up for k in _APPROVED_KEYWORDS):
        return "approved", resp
    if status == "declined" or any(k in up for k in _DECLINED_KEYWORDS):
        if not any(k in up for k in _INFRA_ERROR_KEYWORDS):
            return "declined", resp
    if status in ("charged", "approved", "declined"):
        return status, resp
    if any(k in up for k in _INFRA_ERROR_KEYWORDS):
        return "error", resp
    if resp != "UNKNOWN":
        return "declined", resp
    return "error", resp


def normalize_proxy(proxy: str) -> str:
    return auto.normalize_proxy(proxy)


# ══════════════════════════════════════════════════════════════
#  Async card check
# ══════════════════════════════════════════════════════════════
async def check_card_async(cc: str, site: str, proxy: str) -> dict:
    proxy_url = ""
    try:
        proxy_url = normalize_proxy(proxy)
    except Exception:
        pass

    try:
        res = await auto_async.run_checkout_for_card_async(site, cc, proxy_url)
    except Exception as e:
        err_msg = str(e).replace("\n", " ")[:150]
        _mark_dead(site, err_msg)
        return {
            "status": "error", "result": err_msg,
            "amount": "0", "site": site, "receipt_url": "", "card": cc,
        }

    status_map = {
        CheckStatus.CHARGED:  "charged",
        CheckStatus.APPROVED: "approved",
        CheckStatus.DECLINED: "declined",
        CheckStatus.ERROR:    "error",
    }
    status     = status_map.get(res.status, "error")
    result_str = res.status_code or _exc_text(res.error) or "UNKNOWN"
    status, result_str = normalize_result(status, result_str)

    if status == "error":
        _mark_dead(site, result_str)

    if status in ("charged", "approved", "declined"):
        _log.info("%s|%s", cc, result_str)
    return {
        "status":      status,
        "result":      result_str,
        "amount":      res.amount or "0",
        "site":        site,
        "receipt_url": res.receipt_url or "",
        "card":        cc,
    }


# ══════════════════════════════════════════════════════════════
#  Stats & memory guard
# ══════════════════════════════════════════════════════════════
_stats = {
    "active":   0,
    "total":    0,
    "charged":  0,
    "approved": 0,
    "declined": 0,
    "errors":   0,
    "by":       "3ltz",
    "started":  time.strftime("%Y-%m-%d %H:%M:%S"),
}



def _is_memory_exceeded() -> bool:
    if not _MEMORY_CHECK or psutil is None:
        return False
    now = time.time()
    if now - _mem_cache["ts"] < 5.0:
        return _mem_cache["val"]
    try:
        val = psutil.virtual_memory().percent >= MEMORY_LIMIT_PCT
    except Exception:
        val = False
    _mem_cache["val"] = val
    _mem_cache["ts"]  = now
    return val


async def _save_dump(card: str, site: str, status: str, result: str, amount: str):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {status.upper()} | {card} | {site} | {result} | ${amount}\n"
    def _write():
        try:
            with open("dump.txt", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception:
            pass
    await asyncio.to_thread(_write)


# ══════════════════════════════════════════════════════════════
#  FastAPI app
# ══════════════════════════════════════════════════════════════
@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield

app = FastAPI(title="3ltz", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.get("/venoms")
async def route_status():
    return JSONResponse({
        "ok": True, "api": "3ltz",
        **_stats
    })


@app.api_route("/venom", methods=["GET", "POST"])
async def route_check(
    request: Request,
    cc:    Optional[str] = Query(None),
    site:  Optional[str] = Query(None),
    proxy: Optional[str] = Query(None),
):
    # ── فحص الذاكرة أولاً ─────────────────────────────────────
    if _is_memory_exceeded():
        return JSONResponse({"error": "Server is busy"}, status_code=503)

    if request.method == "POST":
        try:
            body  = await request.json()
            cc    = body.get("cc",    cc)
            site  = body.get("site",  site)
            proxy = body.get("proxy", proxy)
        except Exception:
            pass

    if not cc:
        return JSONResponse({"error": "Missing cc"}, status_code=400)
    if not site:
        return JSONResponse({"error": "Missing site"}, status_code=400)

    _stats["active"] += 1
    _stats["total"]  += 1
    t0 = time.monotonic()

    try:
        result = await asyncio.wait_for(
            check_card_async(cc, site, proxy or ""),
            timeout=REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _stats["errors"] += 1
        _stats["active"] -= 1
        _log.info("%s|Timeout", cc)
        return JSONResponse({
            "Status":  "SiteError",
            "Response": "Timeout",
            "Price":   "-",
            "Gateway": "3ltz",
            "Card":    cc,
            "site":    site,
            "elapsed": round(time.monotonic() - t0, 2),
        })
    except Exception as e:
        _stats["errors"] += 1
        _stats["active"] -= 1
        _log.info("%s|%s", cc, str(e)[:80])
        return JSONResponse({
            "Status":  "SiteError",
            "Response": str(e)[:150],
            "Price":   "-",
            "Gateway": "3ltz",
            "Card":    cc,
            "site":    site,
            "elapsed": round(time.monotonic() - t0, 2),
        })

    elapsed     = round(time.monotonic() - t0, 2)
    card_status = result.get("status", "error")

    _stats[{"charged": "charged", "approved": "approved",
            "declined": "declined"}.get(card_status, "errors")] += 1
    _stats["active"] -= 1

    if card_status in ("charged", "approved"):
        await _save_dump(cc, site, card_status,
                         result.get("result", ""), result.get("amount", "0"))

    bot_status = {
        "charged":  "Charged",
        "approved": "Approved",
        "declined": "Declined",
    }.get(card_status, "SiteError")

    _result_str = result.get("result", "")
    if card_status == "charged":
        _result_str = "ORDER_PAID"
    elif card_status == "approved" and "3DS" in _result_str.upper():
        _result_str = "3DS_REQUIRED"

    return JSONResponse({
        "Status":  bot_status,
        "Response": _result_str,
        "Price":   result.get("amount", "-"),
        "Gateway": "3ltz",
        "Card":    cc,
        "site":    site,
        "elapsed": elapsed,
    })


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import multiprocessing
    # ✅ worker واحد لكل CPU — async يعالج كل التزامن داخلياً
    cpu_count = multiprocessing.cpu_count()
    workers   = max(2, cpu_count)

    print("━" * 50)
    print("  3ltz Checker API — TURBO MODE")
    print(f"  Port         : {PORT}")
    print(f"  Workers      : {workers}  (1 per CPU)")
    print(f"  Endpoint     : /venom")
    print(f"  Status       : /venoms")
    print(f"  Timeout      : {REQUEST_TIMEOUT}s")
    print("━" * 50)
    print("━" * 50)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        loop="uvloop",          # ✅ أسرع event loop
        workers=workers,        # ✅ 1 لكل CPU فقط
        access_log=False,
        backlog=4096,
        timeout_keep_alive=55,
        limit_max_requests=None,
    )
