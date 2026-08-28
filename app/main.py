import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(override=True)
ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
AUDIT_FILE = DATA / "audit.jsonl"
MAX_QUANTITY, MAX_ORDER_VALUE = 5, 10_000
app = FastAPI(title="Cartwise")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
orders: dict[str, dict[str, Any]] = {}
sessions: dict[str, dict[str, Any]] = {}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, max_length=100)
class CreateOrderRequest(BaseModel):
    product_id: str
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
class PaymentUpdate(BaseModel):
    status: str
    payment_id: str | None = None
    signature: str | None = None

def now() -> str: return datetime.now(timezone.utc).isoformat()
def audit(action: str, inputs: dict, result: dict) -> None:
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": now(), "action": action, "inputs": inputs, "result": result}) + "\n")
def public(product: dict) -> dict:
    return {k: product[k] for k in ("id", "name", "price", "stock", "category", "color", "sizes", "description")}
def money(amount: int) -> str: return f"₹{amount:,}"
def razorpay_credentials() -> tuple[str | None, str | None]:
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip() or None
    secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip() or None
    return key_id, secret
def product_by_id(product_id: str) -> dict | None: return next((p for p in CATALOG if p["id"] == product_id), None)
def natural_list(items: list[str]) -> str:
    if len(items) < 2: return "".join(items)
    if len(items) == 2: return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]
def quantity_in(text: str) -> int:
    without_budget = re.sub(r"(?:under|below|less than|upto|up to)\s*(?:₹|rs\.?|inr)?\s*[0-9,]+", "", text.lower())
    match = re.search(r"\b(\d{1,4})\s*(?:pairs?|pair|pieces?|units?|x|running|trail|shoes?|socks?|bottles?)\b", without_budget)
    return int(match.group(1)) if match else 1
def price_limit_in(text: str) -> int | None:
    match = re.search(r"(?:under|below|less than|upto|up to)\s*(?:₹|rs\.?|inr)?\s*([0-9,]+)", text.lower())
    return int(match.group(1).replace(",", "")) if match else None

def search_catalog(query: str, max_price: int | None = None) -> dict:
    synonyms = {"shoe":"shoes", "sneaker":"shoes", "sneakers":"shoes", "hydration":"bottle", "sock":"socks", "accessory":"accessories"}
    ignored = {
        "show", "me", "i", "want", "need", "give", "tell", "have", "any",
        "something", "for", "a", "an", "the", "please", "under", "below",
        "than", "price", "cost", "much", "less", "expensive", "cheaper", "of", "with", "pairs", "pair",
        "which", "what", "how", "do", "you", "is", "are", "in", "there", "s", "available",
        "availability", "stock", "sizes", "size", "compare", "comparison",
        "difference", "between", "and", "cheapest", "lowest",
    }
    terms = {synonyms.get(w, w) for w in re.findall(r"[a-z]+", query.lower())} - ignored
    matches = [public(p) for p in CATALOG if (not terms or all(t in " ".join(str(v).lower() for v in p.values()) for t in terms)) and (max_price is None or p["price"] <= max_price)]
    status = "no_match" if not matches else "ambiguous" if len(matches) > 1 else "exact_match"
    result = {"status": status, "matches": matches, "available_categories": sorted({p["category"] for p in CATALOG})}
    audit("search_catalog", {"query": query, "max_price": max_price}, {"status": status, "matches": len(matches), "product_ids": [p["id"] for p in matches]})
    return result

def match_product(text: str) -> dict | None:
    lowered = text.lower()
    for product in CATALOG:
        if product["id"].lower() in lowered or product["name"].lower() in lowered: return product
    colors = [p for p in CATALOG if p["color"] in lowered]
    return colors[0] if colors and any(w in lowered for w in ("shoe", "one", "pair", "blue", "black", "red", "green")) else None

def request_intent(text: str) -> str | None:
    lowered = text.lower()
    if any(word in lowered for word in ("compare", "comparison", "difference", " versus ", " vs ")):
        return "compare"
    if any(word in lowered for word in ("cheapest", "cheaper", "lowest price", "least expensive", "cost less")):
        return "cheapest"
    if any(word in lowered for word in ("size", "sizes", "fit")):
        return "sizes"
    if any(word in lowered for word in ("in stock", "available", "availability", "stock")):
        return "availability"
    if any(word in lowered for word in ("price", "cost", "how much")):
        return "price"
    return None

def product_facts(product: dict, intent: str) -> str:
    availability = f"{product['stock']} in stock" if product["stock"] else "currently out of stock"
    if intent == "sizes":
        return f"{product['name']} is available in sizes {natural_list([str(size) for size in product['sizes']])}. It is {availability}."
    if intent == "availability":
        return f"{product['name']} is {availability}."
    return f"{product['name']} costs {money(product['price'])}. It is {availability}."

def comparison_facts(products: list[dict]) -> str:
    details = "; ".join(
        f"{product['name']}: {money(product['price'])}, sizes {natural_list([str(size) for size in product['sizes']])}, "
        f"{'in stock' if product['stock'] else 'out of stock'}"
        for product in products
    )
    return f"Here is the comparison: {details}."

def compared_products(text: str) -> list[dict]:
    lowered = text.lower()
    selected = [product for product in CATALOG if product["name"].lower() in lowered]
    selected.extend(product for product in CATALOG if product["color"] in re.findall(r"[a-z]+", lowered))
    return list({product["id"]: product for product in selected}.values())

def create_checkout_order(product_id: str, quantity: int, session_id: str | None = None) -> dict:
    product = product_by_id(product_id)
    if not product:
        audit("guardrail_blocked", {"product_id": product_id, "quantity": quantity}, {"reason": "unknown_product"})
        raise HTTPException(404, "That product is not in the catalog and cannot be charged.")
    if quantity > MAX_QUANTITY:
        audit("guardrail_blocked", {"product_id": product_id, "quantity": quantity}, {"reason":"max_quantity", "limit":MAX_QUANTITY})
        return {"ok":False, "reason":"max_quantity"}
    if product["stock"] < quantity:
        audit("create_order", {"product_id":product_id,"quantity":quantity}, {"ok":False,"reason":"out_of_stock","stock":product["stock"]})
        return {"ok":False,"reason":"out_of_stock","product":public(product)}
    amount = product["price"] * quantity
    if amount > MAX_ORDER_VALUE:
        audit("guardrail_blocked", {"product_id":product_id,"quantity":quantity,"amount":amount}, {"reason":"max_order_value","limit":MAX_ORDER_VALUE})
        return {"ok":False,"reason":"max_order_value"}
    if session_id:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        for old in orders.values():
            if old.get("session_id") == session_id and old["product"]["id"] == product_id and old["quantity"] == quantity and datetime.fromisoformat(old["created_at"]) > cutoff:
                audit("guardrail_blocked", {"product_id":product_id,"quantity":quantity}, {"reason":"duplicate_order","order_id":old["id"]})
                return {"ok":False,"reason":"duplicate_order","order":old}
    local_id = f"local_{uuid.uuid4().hex[:12]}"
    order = {"id":local_id,"product":public(product),"quantity":quantity,"amount":amount,"status":"created","provider":"demo","created_at":now(),"session_id":session_id}
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip() or None
    secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip() or None
    if key_id and secret:
        try:
            import razorpay
            remote = razorpay.Client(auth=(key_id, secret)).order.create({"amount":amount*100,"currency":"INR","receipt":local_id})
            order.update({"id":remote["id"],"provider":"razorpay"})
        except Exception as exc:
            error_type = type(exc).__name__
            error_message = str(exc).strip() or "no error message provided"
            audit("create_order", {"product_id":product_id,"quantity":quantity,"amount":amount}, {"ok":False,"reason":"razorpay_order_failed","error_type":error_type,"error_message":error_message})
            raise HTTPException(502, f"Razorpay order creation failed ({error_type}): {error_message}") from exc
    orders[order["id"]] = order
    audit("create_order", {"product_id":product_id,"quantity":quantity,"amount":amount}, {"ok":True,"order_id":order["id"],"provider":order["provider"]})
    return {"ok":True,"order":order,"razorpay_key":key_id if order["provider"] == "razorpay" else None}

def response(message: str, session_id: str) -> dict:
    text, lower, state = message.strip(), message.strip().lower(), sessions.setdefault(session_id, {})
    qty = quantity_in(text)
    if qty > MAX_QUANTITY:
        audit("guardrail_blocked", {"message":text,"quantity":qty}, {"reason":"max_quantity","limit":MAX_QUANTITY})
        return {"reply":f"I can place up to {MAX_QUANTITY} units per order to protect inventory and payment limits. Would you like {MAX_QUANTITY} or fewer?", "products":[],"order":None}
    if any(w in lower for w in ("hi", "hello", "hey")) and len(lower.split()) <= 3:
        categories = natural_list(sorted({p["category"] for p in CATALOG}))
        return {"reply":f"Welcome to Cartwise! I can help you find {categories}. What are you shopping for today?", "products":[],"order":None}
    payment_words = ("payment link", "pay now", "checkout", "complete payment", "pay for it")
    confirms = lower in {"yes","yes please","confirm","confirmed","go ahead","buy it","buy"} or any(p in lower for p in payment_words)
    if confirms and state.get("pending"):
        pending = state.pop("pending")
        result = create_checkout_order(pending["product_id"], pending["quantity"], session_id)
        if result["ok"]:
            order = result["order"]; addon = next((p for p in CATALOG if p["category"] == "accessories" and p["stock"] > 0), None)
            extra = f" After payment, you may also like {addon['name']} for {money(addon['price'])}." if addon else ""
            checkout_message = "Razorpay is temporarily unavailable, so demo checkout is shown below." if order.get("fallback_reason") else "Open secure checkout below."
            return {"reply":f"Your order for {order['quantity']} × {order['product']['name']} ({money(order['amount'])}) is ready. {checkout_message}{extra}","products":[],"summary":{"product":order["product"],"quantity":order["quantity"],"amount":order["amount"]},"order":order,"razorpay_key":result["razorpay_key"]}
        if result["reason"] == "out_of_stock":
            alternatives = [public(p) for p in CATALOG if p["category"] == result["product"]["category"] and p["stock"] > 0]
            return {"reply":f"{result['product']['name']} is out of stock. Here are available alternatives.","products":alternatives,"order":None}
        if result["reason"] == "duplicate_order": return {"reply":f"A matching order ({result['order']['id']}) already exists, so I did not charge you twice.","products":[],"order":None}
    if any(p in lower for p in payment_words):
        choices = state.get("choices", [])
        if choices:
            names = [product_by_id(product_id)["name"] for product_id in choices if product_by_id(product_id)]
            return {"reply":f"I found more than one option. Please choose {natural_list(names)} by tapping a card or typing its full name, then I can prepare checkout.","products":[],"order":None}
        return {"reply":"Please choose a product first by tapping its card or typing its name. I’ll then prepare secure checkout after you confirm.","products":[],"order":None}
    intent = request_intent(text)
    if intent == "compare":
        products_to_compare = compared_products(text)
        if len(products_to_compare) >= 2:
            audit("compare_catalog", {"query": text}, {"product_ids": [p["id"] for p in products_to_compare]})
            return {"reply":comparison_facts(products_to_compare),"products":[public(p) for p in products_to_compare],"order":None,"search_status":"comparison"}
    product = match_product(text)
    if product and intent in {"price", "sizes", "availability"}:
        audit("catalog_question", {"query": text, "intent": intent}, {"product_ids": [product["id"]]})
        return {"reply":product_facts(product, intent),"products":[public(product)],"order":None,"search_status":intent}
    if product:
        audit("search_catalog", {"query": text, "max_price": None}, {"status": "exact_match", "matches": 1, "product_ids": [product["id"]]})
        state["pending"] = {"product_id":product["id"],"quantity":qty}
        audit("select_product", {"product_id":product["id"],"quantity":qty}, {"amount":product["price"]*qty,"stock":product["stock"]})
        return {"reply":f"I found {product['name']}: {money(product['price'])} each. {qty} × totals {money(product['price']*qty)}. Say “yes” or “give me the payment link” to confirm.","products":[public(product)],"order":None}
    limit = price_limit_in(text)
    search = search_catalog(text, limit)
    if intent == "cheapest" and search["matches"]:
        in_stock = [p for p in search["matches"] if p["stock"] > 0]
        candidates = in_stock or search["matches"]
        lowest_price = min(p["price"] for p in candidates)
        cheapest = [p for p in candidates if p["price"] == lowest_price]
        names = natural_list([p["name"] for p in cheapest])
        status = "in stock" if in_stock else "currently out of stock"
        return {"reply":f"The cheapest matching {'products are' if len(cheapest) > 1 else 'product is'} {names} at {money(lowest_price)} each. {'They are' if len(cheapest) > 1 else 'It is'} {status}.","products":cheapest,"order":None,"search_status":"cheapest"}
    if intent == "compare" and len(search["matches"]) >= 2:
        return {"reply":comparison_facts(search["matches"]),"products":search["matches"],"order":None,"search_status":"comparison"}
    if intent in {"price", "sizes", "availability"} and search["matches"]:
        if intent == "price":
            reply = "I found matching products. Their prices are shown on the product cards below."
        elif intent == "sizes":
            facts = "; ".join(f"{p['name']}: sizes {natural_list([str(size) for size in p['sizes']])}" for p in search["matches"])
            reply = f"Available sizes: {facts}."
        else:
            facts = "; ".join(f"{p['name']}: {'in stock (' + str(p['stock']) + ')' if p['stock'] else 'out of stock'}" for p in search["matches"])
            reply = f"Availability: {facts}."
        audit("catalog_question", {"query": text, "intent": intent}, {"product_ids": [p["id"] for p in search["matches"]]})
        return {"reply":reply,"products":search["matches"],"order":None,"search_status":intent}
    if search["status"] == "ambiguous":
        state["choices"] = [p["id"] for p in search["matches"]]
        choices = "; ".join(f"{p['name']} ({p['color']}, {money(p['price'])}, {'in stock' if p['stock'] else 'out of stock'})" for p in search["matches"])
        return {"reply":f"I found multiple matching products: {choices}. Tap a product card or choose one by its full name or colour.","products":search["matches"],"order":None,"search_status":"ambiguous"}
    if search["status"] == "exact_match":
        product = search["matches"][0]
        state["pending"] = {"product_id":product["id"],"quantity":qty}
        audit("select_product", {"product_id":product["id"],"quantity":qty}, {"amount":product["price"]*qty,"stock":product["stock"]})
        return {"reply":f"I found exactly one match: {product['name']} ({product['color']}, {money(product['price'])}, {'in stock' if product['stock'] else 'out of stock'}). Say yes to confirm before checkout.","products":[product],"order":None,"search_status":"exact_match"}
    categories = search["available_categories"]
    available = [public(p) for p in CATALOG if p["stock"] > 0]
    available_list = "\n".join(f"• {p['name']} — {p['color']} — {money(p['price'])}" for p in available)
    audit("catalog_unavailable_request", {"query":text}, {"available_categories":categories, "available_product_ids":[p["id"] for p in CATALOG if p["stock"] > 0]})
    return {"reply":f"I don’t currently stock that item. I currently carry {natural_list(categories)}.\n\nHere are the available options:\n{available_list}\n\nTap a product below to see its details, or tell me your budget.","products":available,"order":None,"search_status":"no_match"}
    known = {"shoe","shoes","running","trail","sock","socks","bottle","accessories","something","anything"}
    if False:
        results = search_catalog(text, limit)
        if results: return {"reply":f"I found {len(results)} option{'s' if len(results)!=1 else ''}{' within '+money(limit) if limit else ''}. Pick one by name or colour and I’ll prepare the order.","products":results[:4],"order":None}
    categories = sorted({p["category"] for p in CATALOG})
    available = [public(p) for p in CATALOG if p["stock"] > 0]
    available_list = "\n".join(f"• {p['name']} — {p['color']} — {money(p['price'])}" for p in available)
    audit("catalog_unavailable_request", {"query":text}, {"available_categories":categories, "available_product_ids":[p["id"] for p in CATALOG if p["stock"] > 0]})
    return {"reply":f"I don’t currently stock that item. I currently carry {natural_list(categories)}.\n\nHere are the available options:\n{available_list}\n\nTap a product below to see its details, or tell me your budget.","products":available,"order":None}

@app.get("/")
def home(): return FileResponse(ROOT / "static" / "index.html")
@app.get("/api/catalog")
def catalog(): return {"products":[public(p) for p in CATALOG]}
@app.get("/api/audit")
def get_audit():
    if not AUDIT_FILE.exists(): return {"entries":[]}
    return {"entries":[json.loads(x) for x in AUDIT_FILE.read_text(encoding="utf-8").splitlines() if x][-100:][::-1]}
@app.post("/api/orders")
def create_order(request: CreateOrderRequest): return create_checkout_order(request.product_id, request.quantity)
@app.post("/api/orders/{order_id}/payment")
def update_payment(order_id: str, update: PaymentUpdate):
    if order_id not in orders: raise HTTPException(404,"Order not found")
    if update.status not in {"paid","failed"}: raise HTTPException(400,"Invalid payment status")
    order = orders[order_id]
    if update.status == "paid" and order["provider"] == "razorpay":
        if not update.payment_id or not update.signature: raise HTTPException(400,"A Razorpay payment signature is required.")
        try:
            import razorpay
            key_id, secret = razorpay_credentials()
            if not key_id or not secret:
                raise HTTPException(500,"Razorpay credentials are not loaded on the server.")
            razorpay.Client(auth=(key_id,secret)).utility.verify_payment_signature({"razorpay_order_id":order_id,"razorpay_payment_id":update.payment_id,"razorpay_signature":update.signature})
        except Exception as exc:
            audit("check_payment_status", {"order_id":order_id,"payment_id":update.payment_id}, {"status":"signature_verification_failed"})
            raise HTTPException(400,"Payment verification failed.") from exc
    order["status"] = update.status
    audit("check_payment_status", {"order_id":order_id,"amount":order["amount"]}, {"status":update.status,"provider":order["provider"]})
    return order
@app.post("/api/chat")
def chat(request: ChatRequest): return response(request.message, request.session_id or "anonymous")

