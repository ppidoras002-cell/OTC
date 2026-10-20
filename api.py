import os, hmac, hashlib, json, secrets, time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

import httpx
import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
DB=os.getenv("DB_PATH","otc.db")
BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
CRYPTOPAY_TOKEN=os.getenv("CRYPTOPAY_TOKEN","").strip()
CRYPTOPAY_BASE_URL=os.getenv("CRYPTOPAY_BASE_URL","https://pay.crypt.bot/api").rstrip("/")
CRYPTOPAY_WEBHOOK_SECRET=os.getenv("CRYPTOPAY_WEBHOOK_SECRET","").strip()
PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL","").rstrip("/")
INTERNAL_API_SECRET=os.getenv("INTERNAL_API_SECRET","").strip()
RUB_PAYMENT_DETAILS=os.getenv("RUB_PAYMENT_DETAILS","Оплата СБП/картой согласовывается с администратором.").strip()
ADMIN_IDS={int(x) for x in os.getenv("ADMIN_IDS","").replace(" ","").split(",") if x.isdigit()}
DEFAULT_COMMISSION=float(os.getenv("COMMISSION_PERCENT","5"))
MIN_AMOUNT_RUB=float(os.getenv("MIN_AMOUNT_RUB","100"))
MIN_AMOUNT_USDT=float(os.getenv("MIN_AMOUNT_USDT","1"))
app=FastAPI(title="OTC Guarant API")
app.mount("/static", StaticFiles(directory="web"), name="static")

TRANSITIONS={
 "created":{"awaiting_payment","cancelled"},
 "awaiting_payment":{"paid_review","awaiting_transfer","cancelled"},
 "paid_review":{"awaiting_transfer","disputed","cancelled"},
 "awaiting_transfer":{"buyer_confirmed","disputed"},
 "buyer_confirmed":{"completed","disputed"},
 "disputed":{"completed","cancelled"},
 "completed":set(),"cancelled":set()
}
def now(): return datetime.now(timezone.utc).isoformat()

async def ensure_column(db, table, name, definition):
    cur=await db.execute(f"PRAGMA table_info({table})")
    cols={r[1] for r in await cur.fetchall()}
    if name not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
          tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS deals(
          id TEXT PRIMARY KEY, seller_id INTEGER NOT NULL, buyer_id INTEGER, title TEXT NOT NULL,
          description TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
          payment_method TEXT NOT NULL, status TEXT NOT NULL, commission REAL NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payment_ref TEXT,
          transfer_note TEXT, dispute_note TEXT)""")
        await ensure_column(db,"deals","invoice_id","TEXT")
        await ensure_column(db,"deals","pay_url","TEXT")
        await ensure_column(db,"deals","paid_amount","TEXT")
        await ensure_column(db,"deals","paid_at","TEXT")
        await db.execute("""CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT NOT NULL, actor_id INTEGER,
          action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('commission_percent',?)",(str(DEFAULT_COMMISSION),))
        await db.execute("CREATE TABLE IF NOT EXISTS worker_modes(tg_id INTEGER PRIMARY KEY, unlocked_at TEXT NOT NULL)")
        await db.commit()

@app.on_event("startup")
async def startup(): await init_db()

@app.get("/")
async def home(): return FileResponse("web/index.html")

def verify_telegram_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401,"Откройте Mini App через Telegram-бота")
    data=dict(parse_qsl(init_data, keep_blank_values=True))
    received=data.pop("hash",None)
    if not received: raise HTTPException(401,"Нет подписи Telegram")
    check="\n".join(f"{k}={v}" for k,v in sorted(data.items()))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    calculated=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated,received):
        raise HTTPException(401,"Неверная подпись Telegram")
    try:
        auth_date=int(data.get("auth_date","0"))
        if time.time()-auth_date>86400 or auth_date>time.time()+60:
            raise HTTPException(401,"Данные Telegram устарели")
        user=json.loads(data["user"])
        return {"id":int(user["id"]),"username":user.get("username",""),"first_name":user.get("first_name","")}
    except (ValueError,KeyError,TypeError,json.JSONDecodeError):
        raise HTTPException(401,"Некорректные данные Telegram")

async def current_user(init_data: Optional[str]):
    if not init_data: raise HTTPException(401,"Откройте приложение через Telegram")
    return verify_telegram_init_data(init_data)

async def commission_percent(db):
    cur=await db.execute("SELECT value FROM settings WHERE key='commission_percent'")
    row=await cur.fetchone()
    try: val=float(row[0]) if row else DEFAULT_COMMISSION
    except (ValueError,TypeError): val=DEFAULT_COMMISSION
    if not 0 <= val <= 30: return DEFAULT_COMMISSION
    return val

async def log_event(db, deal_id, actor, action, details=""):
    await db.execute("INSERT INTO events(deal_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",
                     (deal_id,actor,action,details,now()))

async def crypto_request(method, payload):
    if not CRYPTOPAY_TOKEN:
        raise HTTPException(503,"Crypto Pay не настроен: добавьте CRYPTOPAY_TOKEN в переменные окружения")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response=await client.post(f"{CRYPTOPAY_BASE_URL}/{method}",
              headers={"Crypto-Pay-API-Token":CRYPTOPAY_TOKEN,"Content-Type":"application/json"},
              json=payload)
        data=response.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(502,"Не удалось связаться с Crypto Pay API")
    if response.status_code != 200 or not data.get("ok"):
        # Do not leak the token or sensitive upstream response.
        raise HTTPException(502,"Crypto Pay отклонил запрос. Проверьте токен и параметры приложения.")
    return data["result"]

class CreateDeal(BaseModel):
    title: str = Field(min_length=3,max_length=100)
    description: str = Field(min_length=5,max_length=1500)
    amount: float = Field(gt=0,le=100000000)
    currency: str
    payment_method: str

class DealAction(BaseModel):
    action: str
    note: str = Field(default="",max_length=1000)

class CommissionUpdate(BaseModel):
    percent: float = Field(ge=0,le=30)

@app.get("/api/me")
async def me(x_telegram_init_data: Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    async with aiosqlite.connect(DB) as db:
        await db.execute("""INSERT INTO users(tg_id,username,first_name,created_at) VALUES(?,?,?,?)
          ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name""",
          (u["id"],u["username"],u["first_name"],now()))
        fee=await commission_percent(db); await db.commit()
        cur=await db.execute("SELECT 1 FROM worker_modes WHERE tg_id=?",(u["id"],))
        worker_enabled=await cur.fetchone() is not None
    return {"user":u,"commission_percent":fee,"min_amount_rub":MIN_AMOUNT_RUB,
            "min_amount_usdt":MIN_AMOUNT_USDT,"is_admin":u["id"] in ADMIN_IDS,
            "worker_mode_enabled":worker_enabled,"rub_payment_details":RUB_PAYMENT_DETAILS}

class WorkerUnlock(BaseModel):
    telegram_id: int = Field(gt=0)

@app.post("/api/internal/worker-mode/unlock")
async def unlock_worker_mode(body: WorkerUnlock, x_internal_secret: Optional[str]=Header(default=None)):
    # Called only by the Telegram bot after the user explicitly sends /workzima.
    if not INTERNAL_API_SECRET or not x_internal_secret or not hmac.compare_digest(x_internal_secret,INTERNAL_API_SECRET):
        raise HTTPException(403,"Forbidden")
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO worker_modes(tg_id,unlocked_at) VALUES(?,?)",(body.telegram_id,now()))
        await db.commit()
    return {"ok":True,"worker_mode_enabled":True}

@app.post("/api/deals")
async def create_deal(body: CreateDeal, x_telegram_init_data: Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    if body.currency not in {"RUB","USDT"}: raise HTTPException(400,"Валюта должна быть RUB или USDT")
    if body.payment_method not in {"manual_rub","cryptobot"}: raise HTTPException(400,"Неизвестный способ оплаты")
    if body.currency=="RUB" and body.payment_method!="manual_rub": raise HTTPException(400,"Для рублей выберите СБП/карту")
    if body.currency=="USDT" and body.payment_method!="cryptobot": raise HTTPException(400,"Для USDT выберите Crypto Bot")
    minimum=MIN_AMOUNT_RUB if body.currency=="RUB" else MIN_AMOUNT_USDT
    if body.amount < minimum: raise HTTPException(400,f"Минимальная сумма: {minimum:g} {body.currency}")
    did=secrets.token_hex(5).upper(); t=now()
    async with aiosqlite.connect(DB) as db:
        fee=round(body.amount*(await commission_percent(db))/100,8)
        await db.execute("""INSERT INTO deals(id,seller_id,title,description,amount,currency,payment_method,status,commission,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (did,u["id"],body.title.strip(),body.description.strip(),body.amount,body.currency,body.payment_method,"created",fee,t,t))
        await log_event(db,did,u["id"],"created",f"fee={fee} {body.currency}")
        await db.commit()
    return {"id":did,"status":"created","commission":fee,"share_path":f"/?deal={did}"}

@app.get("/api/deals")
async def list_deals(x_telegram_init_data: Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals WHERE seller_id=? OR buyer_id=? ORDER BY created_at DESC LIMIT 100",(u["id"],u["id"]))
        rows=[dict(r) for r in await cur.fetchall()]
    return {"deals":rows}

@app.get("/api/deals/{deal_id}")
async def get_deal(deal_id:str,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data); did=deal_id.upper()
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals WHERE id=?",(did,)); row=await cur.fetchone()
        if not row: raise HTTPException(404,"Сделка не найдена")
        d=dict(row)
        if u["id"] not in {d["seller_id"],d["buyer_id"]} and u["id"] not in ADMIN_IDS:
            if d["buyer_id"] is None and d["seller_id"] != u["id"]:
                status="awaiting_payment" if d["status"]=="created" else d["status"]
                await db.execute("UPDATE deals SET buyer_id=?,status=?,updated_at=? WHERE id=? AND buyer_id IS NULL",
                                 (u["id"],status,now(),did))
                await log_event(db,did,u["id"],"buyer_joined")
                await db.commit(); d["buyer_id"]=u["id"]; d["status"]=status
            else: raise HTTPException(403,"Это не ваша сделка")
    return {"deal":d,"rub_payment_details":RUB_PAYMENT_DETAILS}

@app.post("/api/deals/{deal_id}/create-invoice")
async def create_invoice(deal_id:str,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data); did=deal_id.upper()
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals WHERE id=?",(did,)); row=await cur.fetchone()
        if not row: raise HTTPException(404,"Сделка не найдена")
        d=dict(row)
        if u["id"]!=d["buyer_id"]: raise HTTPException(403,"Оплатить может только покупатель")
        if d["currency"]!="USDT" or d["payment_method"]!="cryptobot": raise HTTPException(400,"Эта сделка не оплачивается через Crypto Bot")
        if d["status"]!="awaiting_payment": raise HTTPException(400,"Сделка не ожидает оплаты")
        if d["invoice_id"] and d["pay_url"]:
            return {"invoice_id":d["invoice_id"],"pay_url":d["pay_url"],"status":"existing"}
        # Store payload with a random nonce so the webhook is linked to this exact deal/invoice.
        payload=f"otc:{did}:{secrets.token_urlsafe(12)}"
        amount=f"{float(d['amount']):.2f}"
        result=await crypto_request("createInvoice",{
          "currency_type":"crypto","asset":"USDT","amount":amount,
          "description":f"OTC deal #{did}"[:1024],"payload":payload,
          "allow_comments":False,"allow_anonymous":False
        })
        invoice_id=str(result.get("invoice_id","")); pay_url=result.get("bot_invoice_url") or result.get("pay_url") or result.get("mini_app_invoice_url")
        if not invoice_id or not pay_url: raise HTTPException(502,"Crypto Pay не вернул ссылку на оплату")
        await db.execute("UPDATE deals SET invoice_id=?,pay_url=?,payment_ref=?,updated_at=? WHERE id=?",
                         (invoice_id,pay_url,payload,now(),did))
        await log_event(db,did,u["id"],"crypto_invoice_created",f"invoice_id={invoice_id}")
        await db.commit()
    return {"invoice_id":invoice_id,"pay_url":pay_url,"status":"active"}

@app.post("/api/deals/{deal_id}/actions")
async def deal_action(deal_id:str,body:DealAction,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data); did=deal_id.upper()
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals WHERE id=?",(did,)); row=await cur.fetchone()
        if not row: raise HTTPException(404,"Сделка не найдена")
        d=dict(row); admin=u["id"] in ADMIN_IDS; a=body.action
        if u["id"] not in {d["seller_id"],d["buyer_id"]} and not admin: raise HTTPException(403,"Нет доступа")
        if a=="mark_paid":
            if u["id"]!=d["buyer_id"] and not admin: raise HTTPException(403,"Только покупатель может сообщить об оплате")
            if d["payment_method"]!="manual_rub": raise HTTPException(400,"Для Crypto Bot оплата подтверждается автоматически вебхуком")
            if d["status"]!="awaiting_payment": raise HTTPException(400,"Неверный статус")
            target="paid_review"
        elif a=="confirm_payment":
            if not admin: raise HTTPException(403,"Только администратор")
            if d["payment_method"]!="manual_rub" or d["status"]!="paid_review": raise HTTPException(400,"Нет ручного платежа на проверке")
            target="awaiting_transfer"
        elif a=="transfer_sent":
            if u["id"]!=d["seller_id"] and not admin: raise HTTPException(403,"Только продавец может отметить передачу")
            if d["status"]!="awaiting_transfer": raise HTTPException(400,"Оплата ещё не подтверждена")
            target="buyer_confirmed"
        elif a=="confirm_received":
            if u["id"]!=d["buyer_id"] and not admin: raise HTTPException(403,"Только покупатель")
            if d["status"]!="buyer_confirmed": raise HTTPException(400,"Передача ещё не отмечена")
            target="completed"
        elif a=="dispute":
            if d["status"] in {"completed","cancelled"}: raise HTTPException(400,"Сделка уже закрыта")
            target="disputed"
        elif a=="cancel":
            if u["id"]!=d["seller_id"] and not admin: raise HTTPException(403,"Отменить может продавец или администратор")
            if d["status"] not in {"created","awaiting_payment"}: raise HTTPException(400,"Эту сделку нельзя отменить автоматически")
            target="cancelled"
        else: raise HTTPException(400,"Неизвестное действие")
        if target not in TRANSITIONS.get(d["status"],set()): raise HTTPException(400,"Недопустимый переход статуса")
        await db.execute("UPDATE deals SET status=?,updated_at=?,dispute_note=CASE WHEN ?='disputed' THEN ? ELSE dispute_note END WHERE id=?",
                         (target,now(),target,body.note,did))
        await log_event(db,did,u["id"],a,body.note); await db.commit()
    return {"ok":True,"status":target}

@app.get("/api/admin/deals")
async def admin_deals(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    if u["id"] not in ADMIN_IDS: raise HTTPException(403,"Только администратор")
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals ORDER BY created_at DESC LIMIT 200")
        return {"deals":[dict(r) for r in await cur.fetchall()]}

@app.get("/api/admin/settings")
async def get_admin_settings(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    if u["id"] not in ADMIN_IDS: raise HTTPException(403,"Только администратор")
    async with aiosqlite.connect(DB) as db:
        return {"commission_percent":await commission_percent(db),"rub_payment_details":RUB_PAYMENT_DETAILS}

@app.put("/api/admin/settings/commission")
async def set_commission(body:CommissionUpdate,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=await current_user(x_telegram_init_data)
    if u["id"] not in ADMIN_IDS: raise HTTPException(403,"Только администратор")
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES('commission_percent',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(body.percent),))
        await log_event(db,"SYSTEM",u["id"],"commission_updated",f"{body.percent}%")
        await db.commit()
    return {"ok":True,"commission_percent":body.percent}

@app.post("/webhooks/cryptopay/{secret_path}")
async def cryptopay_webhook(secret_path:str,request:Request):
    expected=os.getenv("CRYPTOPAY_WEBHOOK_PATH_SECRET","").strip()
    if not expected or not hmac.compare_digest(secret_path,expected):
        raise HTTPException(404,"Not found")
    if not CRYPTOPAY_TOKEN: raise HTTPException(503,"Crypto Pay not configured")
    raw=await request.body()
    signature=request.headers.get("crypto-pay-api-signature","")
    secret=hashlib.sha256(CRYPTOPAY_TOKEN.encode()).digest()
    calculated=hmac.new(secret,raw,hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(calculated,signature):
        raise HTTPException(401,"Invalid Crypto Pay signature")
    try: update=json.loads(raw)
    except json.JSONDecodeError: raise HTTPException(400,"Invalid JSON")
    if update.get("update_type")!="invoice_paid": return {"ok":True,"ignored":True}
    inv=update.get("payload") or {}
    invoice_id=str(inv.get("invoice_id",""))
    payload=str(inv.get("payload",""))
    if inv.get("status")!="paid" or not invoice_id or not payload.startswith("otc:"):
        return {"ok":True,"ignored":True}
    parts=payload.split(":")
    if len(parts)<3: return {"ok":True,"ignored":True}
    did=parts[1].upper()
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM deals WHERE id=?",(did,)); row=await cur.fetchone()
        if not row: return {"ok":True,"ignored":True}
        d=dict(row)
        # Idempotent: Crypto Pay can retry webhook deliveries.
        if str(d.get("invoice_id") or "")!=invoice_id or d.get("payment_ref")!=payload:
            return {"ok":True,"ignored":True}
        if d["currency"]!="USDT" or d["payment_method"]!="cryptobot":
            return {"ok":True,"ignored":True}
        if d["status"] in {"awaiting_transfer","buyer_confirmed","completed"}:
            return {"ok":True,"duplicate":True}
        if d["status"]!="awaiting_payment":
            return {"ok":True,"ignored":True}
        # Verify the asset and amount before changing the deal state.
        try: paid_amount=float(inv.get("amount","0"))
        except (TypeError,ValueError): paid_amount=0
        if inv.get("asset")!="USDT" or paid_amount+1e-8<float(d["amount"]):
            await log_event(db,did,None,"crypto_invoice_mismatch",f"asset={inv.get('asset')} amount={inv.get('amount')}")
            await db.commit()
            return {"ok":True,"review_required":True}
        await db.execute("UPDATE deals SET status='awaiting_transfer',paid_amount=?,paid_at=?,updated_at=? WHERE id=?",
                         (str(inv.get("amount")),now(),now(),did))
        await log_event(db,did,None,"crypto_payment_confirmed",f"invoice_id={invoice_id}; amount={inv.get('amount')} USDT")
        await db.commit()
    return {"ok":True}

@app.get("/health")
async def health(): return {"ok":True}
