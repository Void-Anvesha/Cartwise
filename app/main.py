import json, os, re, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(override=False)
ROOT=Path(__file__).resolve().parent.parent; DATA=ROOT/"data"; DATA.mkdir(exist_ok=True)
CATALOG=json.loads((ROOT/"catalog.json").read_text(encoding="utf-8"))
AUDIT,ORDERS,ATTEMPTS,CONFIG,CAMPAIGN=DATA/"audit.jsonl",DATA/"orders.json",DATA/"attempts.json",DATA/"merchant_config.json",DATA/"campaign.json"

def read(path, default):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError):return default
orders:dict[str,dict[str,Any]]=read(ORDERS,{}); attempts:dict[str,dict[str,Any]]=read(ATTEMPTS,{}); sessions={}
app=FastAPI(title="Cartwise",description="Conversational and agent-to-agent commerce API")
app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")

class ChatRequest(BaseModel):
    message:str=Field(min_length=1,max_length=1000); session_id:str|None=None
class CreateOrderRequest(BaseModel):
    product_id:str; quantity:int=Field(ge=1); size:int|str|None=None; reasoning:str|None=Field(default=None,max_length=500)
    upsell_source:bool=False; suggested_after:str|None=None; actor:Literal["autonomous_agent"]="autonomous_agent"
class PaymentUpdate(BaseModel):
    status:str; payment_id:str|None=None; signature:str|None=None
class MerchantConfig(BaseModel):
    max_order_value:int=Field(ge=1,le=10_000_000); max_quantity:int=Field(ge=1,le=1000)
class BrowserEvent(BaseModel):
    event:str
    order_id:str|None=None
    detail:str|None=Field(default=None,max_length=500)
class AgentDecisionRequest(BaseModel):
    goal:str=Field(min_length=3,max_length=1000)
    products:list[dict[str,Any]]|None=None

def now():return datetime.now(timezone.utc).isoformat()
def limits():return {"max_quantity":5,"max_order_value":10000,**read(CONFIG,{})}
def save_orders():ORDERS.write_text(json.dumps(orders,indent=2,ensure_ascii=False),encoding="utf-8")
def save_attempts():ATTEMPTS.write_text(json.dumps(attempts,indent=2,ensure_ascii=False),encoding="utf-8")
def reject_attempt(failure_reason,inputs):
    attempt_id=f"attempt_{uuid.uuid4().hex[:12]}"
    attempts[attempt_id]={"id":attempt_id,"status":"rejected","failure_reason":failure_reason,"created_at":now(),"inputs":inputs}
    save_attempts()
def audit_category(tool,result):
    if tool in {"checkout_rendered","razorpay_sdk_loaded","payment_attempted","checkout_dismissed","checkout_error","create_order_called","razorpay_order_created","select_product"}:return "technical"
    if tool=="search_catalog":return "business" if result.get("status") in {"no_match","ambiguous"} else "technical"
    if tool=="check_payment_status":return "business" if result.get("status") in {"completed","paid","failed","signature_verification_failed"} else "technical"
    return "business"
def log(tool,inputs,result,reasoning,category=None):
    entry={"timestamp":now(),"tool":tool,"action":tool,"actor":inputs.get("actor","system"),"input":inputs,"inputs":inputs,"reasoning":reasoning,"result":result,"category":category or audit_category(tool,result)}
    with AUDIT.open("a",encoding="utf-8") as f:f.write(json.dumps(entry,ensure_ascii=False)+"\n")
def product(pid):return next((p for p in CATALOG if p["id"]==pid),None)
def public(p):
    keys=("id","name","category","price","stock","color","sizes","description")
    return {**{k:p[k] for k in keys},"currency":"INR","upsell_with":p.get("upsell_with",[])}
def money(n):return f"₹{n:,}"
def qty(text):
    clean=re.sub(r"(?:under|below|less than|up to)\s*(?:₹|rs\.?|inr)?\s*[0-9,]+","",text.lower())
    m=re.search(r"\b(\d{1,4})\s*(?:pairs?|pieces?|units?|x|shoes?|socks?|bottles?)\b",clean); return int(m.group(1)) if m else 1
def budget(text):
    m=re.search(r"(?:under|below|less than|up to)\s*(?:₹|rs\.?|inr)?\s*([0-9,]+)",text.lower()); return int(m.group(1).replace(",","")) if m else None

def call_gemini(prompt,json_mode=False):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key:raise HTTPException(503,"GEMINI_API_KEY is not configured on the server")
    try:
        from google import genai
        config={"response_mime_type":"application/json"} if json_mode else None
        response=genai.Client(api_key=key).models.generate_content(model=os.getenv("GEMINI_MODEL","gemini-2.5-flash"),contents=prompt,config=config)
        if not response.text:raise ValueError("Gemini returned an empty response")
        return response.text.strip()
    except HTTPException:raise
    except Exception as exc:raise HTTPException(502,f"Gemini request failed: {exc}") from exc

def autonomous_decision(goal):
    available=[public(p) for p in CATALOG if p["stock"]>0]
    prompt=f'''You are an autonomous shopping agent. Select exactly one product that best satisfies the user's goal.
Use only the supplied catalog. Quantity must be 1. Size must be a listed size, or null only when the product has no sizes.
Return only JSON with this shape: {{"product_id":"...","quantity":1,"size":9,"reasoning":"..."}}
Goal: {goal}
Catalog: {json.dumps(available,ensure_ascii=False)}'''
    try:decision=json.loads(call_gemini(prompt,json_mode=True))
    except json.JSONDecodeError as exc:raise HTTPException(502,"Gemini returned invalid decision JSON") from exc
    selected=product(decision.get("product_id")); quantity=decision.get("quantity"); size=decision.get("size")
    if not selected or selected["stock"]<1:raise HTTPException(502,"Gemini selected an unavailable product")
    if quantity!=1:raise HTTPException(502,"Gemini returned an invalid quantity")
    if size is not None and str(size) not in {str(x) for x in selected["sizes"]}:raise HTTPException(502,"Gemini returned an invalid size")
    if not isinstance(decision.get("reasoning"),str) or not decision["reasoning"].strip():raise HTTPException(502,"Gemini omitted its reasoning")
    return {"product_id":selected["id"],"quantity":1,"size":size,"reasoning":decision["reasoning"].strip()}

def search_catalog(query,max_price=None):
    synonyms={"shoe":"shoes","sneaker":"shoes","sneakers":"shoes","hydration":"bottle","sock":"socks"}
    ignored=set("show me i want need any something for a an the please under below than price cost much less of with pair pairs which what how do you is are in there available availability stock sizes size compare comparison difference between and cheapest".split())
    terms={synonyms.get(w,w) for w in re.findall(r"[a-z]+",query.lower())}-ignored
    found=[public(p) for p in CATALOG if (not terms or all(t in " ".join(map(lambda v:str(v).lower(),p.values())) for t in terms)) and (max_price is None or p["price"]<=max_price)]
    status="no_match" if not found else "ambiguous" if len(found)>1 else "exact_match"
    log("search_catalog",{"query":query,"max_price":max_price},{"status":status,"product_ids":[p["id"] for p in found]},f"The customer asked for {query!r}; catalog search grounds the answer in merchant inventory.")
    return status,found

def named_product(text):
    low=text.lower()
    for p in CATALOG:
        if p["id"].lower() in low or p["name"].lower() in low:return p
    colors=[p for p in CATALOG if p["color"] in low]
    return colors[0] if colors and any(w in low for w in ("shoe","one","pair","blue","black","red","green")) else None

def upsell_for(product_id):
    base=product(product_id)
    if not base:return None
    return next((product(candidate) for candidate in base.get("upsell_with",[]) if product(candidate) and product(candidate)["stock"]>0),None)

def create_checkout_order(product_id,quantity,session_id=None,size=None,reasoning=None,upsell_source=False,suggested_after=None,actor="autonomous_agent"):
    p=product(product_id); inp={"product_id":product_id,"quantity":quantity,"size":size,"actor":actor}; guard=limits()
    reason=reasoning or f"The autonomous buyer selected {product_id} and requested {quantity} unit(s) through the commerce API."
    log("create_order_called",inp,{"validation":"started"},reason)
    if not p:
        reject_attempt("unknown_product",inp); log("guardrail_blocked",inp,{"reason":"unknown_product"},"The product is absent from the server-owned catalog."); raise HTTPException(404,"Product not found")
    if size is not None and str(size) not in {str(x) for x in p["sizes"]}:
        out={"ok":False,"reason":"invalid_size","available_sizes":p["sizes"]}; reject_attempt("invalid_size",inp); log("guardrail_blocked",inp,out,"The requested size is not offered for this product."); return out
    if quantity>guard["max_quantity"]:
        out={"ok":False,"reason":"max_quantity","limit":guard["max_quantity"]}; reject_attempt("guardrail_max_quantity",inp); log("guardrail_blocked",inp,out,"The quantity exceeds the merchant-configured per-item limit."); return out
    if p["stock"]<quantity:
        out={"ok":False,"reason":"out_of_stock","stock":p["stock"]}; reject_attempt("stock_unavailable",inp); log("create_order",inp,out,"Available inventory cannot satisfy the requested quantity."); return out
    amount=p["price"]*quantity; inp["amount"]=amount
    if amount>guard["max_order_value"]:
        out={"ok":False,"reason":"max_order_value","limit":guard["max_order_value"]}; reject_attempt("guardrail_max_value",inp); log("guardrail_blocked",inp,out,"The server-calculated total exceeds the merchant approval limit."); return out
    if session_id:
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=5)
        old=next((o for o in orders.values() if o.get("session_id")==session_id and o["product"]["id"]==product_id and o["quantity"]==quantity and datetime.fromisoformat(o["created_at"])>cutoff),None)
        if old:
            out={"ok":False,"reason":"duplicate_order","order":old}; log("guardrail_blocked",inp,{"reason":"duplicate_order","order_id":old["id"]},"A matching recent chat order exists, preventing a duplicate charge."); return out
    local=f"local_{uuid.uuid4().hex[:12]}"
    order={"id":local,"product":public(p),"quantity":quantity,"size":size,"amount":amount,"currency":"INR","status":"created","provider":"demo","created_at":now(),"session_id":session_id,"actor":actor,"upsell_source":upsell_source,"suggested_after":suggested_after}
    key,secret=os.getenv("RAZORPAY_KEY_ID","").strip(),os.getenv("RAZORPAY_KEY_SECRET","").strip()
    if key and secret and "your_" not in key and "your_" not in secret and os.getenv("CARTWISE_DEMO_MODE","").lower() not in {"1","true","yes"}:
        try:
            import razorpay
            remote=razorpay.Client(auth=(key,secret)).order.create({"amount":amount*100,"currency":"INR","receipt":local}); order.update(id=remote["id"],provider="razorpay")
            log("razorpay_order_created",inp,{"order_id":remote["id"],"status":remote.get("status","created")},"Razorpay accepted the validated, server-priced order and returned its real test-mode ID.")
        except Exception as exc:
            log("create_order",inp,{"ok":False,"reason":"razorpay_order_failed","error":str(exc)},"The payment provider rejected creation of the server-priced order."); raise HTTPException(502,f"Razorpay order creation failed: {exc}") from exc
    orders[order["id"]]=order; save_orders()
    log("create_order",{**inp,"upsell_source":upsell_source,"suggested_after":suggested_after},{"ok":True,"order_id":order["id"],"provider":order["provider"]},reason)
    suggestion=upsell_for(product_id)
    return {"ok":True,"order":order,"razorpay_key":key if order["provider"]=="razorpay" else None,"upsell_suggestion":public(suggestion) if suggestion else None}

def create_bundle_order(base_order_id,addon_id,reasoning):
    if base_order_id not in orders:raise HTTPException(404,"Base order not found")
    base=orders[base_order_id]; addon=product(addon_id)
    if base["status"]!="created":return {"ok":False,"reason":"base_order_not_pending"}
    if not addon or addon["stock"]<1:return {"ok":False,"reason":"upsell_out_of_stock"}
    amount=base["amount"]+addon["price"]
    if amount>limits()["max_order_value"]:
        inp={"base_order_id":base_order_id,"addon_id":addon_id,"amount":amount,"actor":base.get("actor","human_chat")}; out={"ok":False,"reason":"max_order_value","limit":limits()["max_order_value"]}; reject_attempt("guardrail_max_value",inp); log("guardrail_blocked",inp,out,"The combined base and add-on total exceeds the merchant approval limit."); return out
    local=f"local_{uuid.uuid4().hex[:12]}"; key,secret=os.getenv("RAZORPAY_KEY_ID","").strip(),os.getenv("RAZORPAY_KEY_SECRET","").strip()
    bundled={**base,"id":local,"amount":amount,"status":"created","created_at":now(),"upsell_source":True,"upsell_accepted":True,"suggested_after":base["product"]["id"],"upsell_source_product":base["product"]["id"],"upsell_added_product":addon_id,"upsell_added_amount":addon["price"],"items":[{"product":base["product"],"quantity":base["quantity"],"size":base.get("size")},{"product":public(addon),"quantity":1,"size":None}],"replaces_order_id":base_order_id}
    if key and secret and "your_" not in key and "your_" not in secret and os.getenv("CARTWISE_DEMO_MODE","").lower() not in {"1","true","yes"}:
        try:
            import razorpay
            remote=razorpay.Client(auth=(key,secret)).order.create({"amount":amount*100,"currency":"INR","receipt":local}); bundled.update(id=remote["id"],provider="razorpay")
            log("razorpay_order_created",{"base_order_id":base_order_id,"addon_id":addon_id,"amount":amount},{"order_id":remote["id"]},"Razorpay created a replacement order containing the accepted add-on in its combined total.")
        except Exception as exc:raise HTTPException(502,f"Razorpay bundle order creation failed: {exc}") from exc
    base["status"]="superseded"; base["superseded_by"]=bundled["id"]; orders[bundled["id"]]=bundled; save_orders()
    log("accept_upsell",{"base_order":base_order_id,"base_product":base["product"]["id"],"added_product":addon_id},{"order_id":bundled["id"],"combined_amount":amount},reasoning)
    return {"ok":True,"order":bundled,"razorpay_key":key if bundled["provider"]=="razorpay" else None}

def chat_reply(message,session_id):
    text=message.strip(); low=text.lower(); state=sessions.setdefault(session_id,{}); quantity=qty(text); base={"products":[],"order":None}; guard=limits()
    if quantity>guard["max_quantity"]:
        log("guardrail_blocked",{"message":text,"quantity":quantity},{"reason":"max_quantity","limit":guard["max_quantity"]},"The customer's quantity exceeds the merchant's current limit."); return {**base,"reply":f"The merchant allows at most {guard['max_quantity']} units per item. Would you like fewer?"}
    if any(w in low for w in ("hi","hello","hey")) and len(low.split())<=3:return {**base,"reply":"Welcome to Cartwise! What are you shopping for?"}
    offered=state.get("upsell")
    accepts_upsell=bool(offered and any(word in low for word in ("add","include","take")) and any(word in low for word in ("sock","bottle",offered["product_id"].lower())))
    if accepts_upsell:
        addon=product(offered["product_id"]); result=create_bundle_order(offered["base_order_id"],addon["id"],f"Customer explicitly accepted the suggested {addon['name']} before paying."); state.pop("upsell",None)
        if not result["ok"]:return {**base,"reply":f"I couldn't add that item: {result['reason'].replace('_',' ')}. Your original checkout is still available."}
        order=result["order"]
        return {**base,"reply":f"Added {addon['name']} for {money(addon['price'])}. Your updated total is {money(order['amount'])}; use the new checkout below.","summary":{"product":order["product"],"quantity":order["quantity"],"amount":order["amount"]},"order":order,"razorpay_key":result["razorpay_key"]}
    confirms=low in {"yes","yes please","confirm","confirmed","go ahead","buy it","buy"} or any(x in low for x in ("payment link","pay now","checkout","complete payment","pay for it"))
    if confirms and state.get("pending"):
        pending=state.pop("pending"); result=create_checkout_order(**pending,session_id=session_id,actor="human_chat",reasoning=f"Customer explicitly confirmed {pending['product_id']} after reviewing its price.")
        if not result["ok"]:return {**base,"reply":f"I couldn't create that order: {result['reason'].replace('_',' ')}. The merchant guardrail was enforced server-side."}
        order=result["order"]; addon=result.get("upsell_suggestion"); extra=""
        if addon:
            state["upsell"]={"product_id":addon["id"],"suggested_after":order["product"]["id"],"base_order_id":order["id"]}; extra=f"\n\nBy the way, {addon['name']} ({money(addon['price'])}) pairs well with {order['product']['name']}. It's optional—say “add {addon['name']}” before paying, or proceed straight to checkout."
            log("suggest_upsell",{"base_product":order["product"]["id"],"suggested_product":addon["id"]},{"shown_to_customer":True},f"{order['product']['name']} explicitly lists {addon['name']} as a complementary product, and it is currently in stock.")
        return {**base,"reply":f"Your order for {order['quantity']} × {order['product']['name']} ({money(order['amount'])}) is ready.{extra}","summary":{"product":order["product"],"quantity":order["quantity"],"amount":order["amount"]},"order":order,"razorpay_key":result["razorpay_key"],"upsell_suggestion":addon}
    p=named_product(text)
    if p:
        if p["stock"]<1:
            alternatives=[public(item) for item in CATALOG if item["stock"]>0 and item["category"]==p["category"]]
            log("guardrail_blocked",{"product_id":p["id"],"stage":"selection"},{"reason":"out_of_stock","alternative_ids":[item["id"] for item in alternatives]},f"{p['name']} has no available inventory, so checkout was blocked before confirmation.")
            return {**base,"reply":f"{p['name']} is currently out of stock, so I can't prepare checkout for it. Here are available alternatives.","products":alternatives}
        up=state.get("upsell"); is_up=bool(up and up["product_id"]==p["id"]); state["pending"]={"product_id":p["id"],"quantity":quantity,"upsell_source":is_up,"suggested_after":up["suggested_after"] if is_up else None}
        log("select_product",{"product_id":p["id"],"quantity":quantity},{"amount":p["price"]*quantity,"stock":p["stock"]},f"The customer named {p['name']}; it was selected for confirmation before checkout.")
        return {**base,"reply":f"{p['name']} costs {money(p['price'])}. {quantity} × totals {money(p['price']*quantity)}. Say yes to confirm.","products":[public(p)]}
    status,found=search_catalog(text,budget(text))
    if status=="no_match":return {**base,"reply":"I couldn't find that in the merchant catalog. Here are available products.","products":[public(p) for p in CATALOG if p["stock"]>0]}
    if "cheapest" in low:
        choices=sorted([p for p in found if p["stock"]>0] or found,key=lambda p:p["price"]); return {**base,"reply":f"The cheapest matching product is {choices[0]['name']} at {money(choices[0]['price'])}.","products":[choices[0]]}
    return {**base,"reply":"I found these catalog-backed options. Choose one by name.","products":found}

@app.get("/")
def home():return FileResponse(ROOT/"static"/"index.html")
@app.get("/merchant")
def merchant_page():return FileResponse(ROOT/"static"/"merchant.html")
@app.get("/api/catalog")
def catalog():return {"merchant":"Talk to Buy Store","products":[public(p) for p in CATALOG]}
@app.post("/api/agent/decide")
def decide_for_agent(request:AgentDecisionRequest):
    decision=autonomous_decision(request.goal)
    log("agent_decide",{"goal":request.goal,"actor":"autonomous_agent"},decision,"Gemini evaluated the live server catalog and selected one product for the autonomous buyer.",category="business")
    return decision
@app.post("/api/order")
@app.post("/api/orders")
def create_order(request:CreateOrderRequest):
    result=create_checkout_order(**request.model_dump())
    return result if result.get("ok") else JSONResponse(status_code=422,content=result)
@app.get("/api/order/{order_id}/status")
def order_status(order_id:str):
    if order_id not in orders:raise HTTPException(404,"Order not found")
    order=orders[order_id]
    if order["provider"]=="razorpay":
        try:
            import razorpay
            remote=razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"),os.getenv("RAZORPAY_KEY_SECRET"))).order.fetch(order_id)
            if remote.get("status")=="paid":order["status"]="completed"; save_orders()
        except Exception:pass
    log("check_payment_status",{"order_id":order_id,"actor":order.get("actor","human_chat")},{"status":order["status"]},"The buyer requested the authoritative payment state for this order."); return order
@app.post("/api/orders/{order_id}/payment")
def update_payment(order_id:str,update:PaymentUpdate):
    if order_id not in orders:raise HTTPException(404,"Order not found")
    if update.status not in {"paid","failed"}:raise HTTPException(400,"Invalid payment status")
    order=orders[order_id]
    if update.status=="paid" and order["provider"]=="razorpay":
        if not update.payment_id or not update.signature:raise HTTPException(400,"A Razorpay payment signature is required")
        try:
            import razorpay
            razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"),os.getenv("RAZORPAY_KEY_SECRET"))).utility.verify_payment_signature({"razorpay_order_id":order_id,"razorpay_payment_id":update.payment_id,"razorpay_signature":update.signature})
        except Exception as exc:
            log("check_payment_status",{"order_id":order_id},{"status":"signature_verification_failed"},"The provider signature could not be verified."); raise HTTPException(400,"Payment verification failed") from exc
    final_status="completed" if update.status=="paid" else "failed"
    order["status"]=final_status; order["completed_at"]=now() if final_status=="completed" else None
    if final_status=="failed":order["failure_reason"]="payment_declined"
    else:order.pop("failure_reason",None)
    save_orders(); log("check_payment_status",{"order_id":order_id,"amount":order["amount"],"actor":order.get("actor","human_chat")},{"status":final_status,"failure_reason":order.get("failure_reason")},"Checkout reported a payment result; the server verified it and persisted the final status used by merchant analytics."); return order
@app.post("/api/audit/event")
def browser_event(event:BrowserEvent):
    allowed={"checkout_rendered","razorpay_sdk_loaded","payment_attempted","checkout_dismissed","checkout_error"}
    if event.event not in allowed:raise HTTPException(400,"Unsupported lifecycle event")
    result={"recorded":True,"detail":event.detail}
    log(event.event,{"order_id":event.order_id},result,f"The storefront reported the {event.event.replace('_',' ')} step for this checkout lifecycle.")
    return result
@app.get("/api/audit")
def get_audit():
    entries=[json.loads(x) for x in AUDIT.read_text(encoding="utf-8").splitlines() if x] if AUDIT.exists() else []
    for entry in entries:
        if not entry.get("reasoning"):
            subject=(entry.get("inputs") or {}).get("product_id") or (entry.get("inputs") or {}).get("query") or "the recorded request"
            entry["reasoning"]=f"This legacy {entry.get('action','commerce')} action was recorded to safely process {subject}."
        entry.setdefault("tool",entry.get("action","commerce_action")); entry.setdefault("input",entry.get("inputs",{}))
        entry.setdefault("category",audit_category(entry["tool"],entry.get("result",{})))
    return {"entries":entries[-100:][::-1]}
@app.get("/api/merchant/summary")
def summary():
    values=[o for o in orders.values() if o.get("status")!="superseded"]; paid=[o for o in values if o["status"] in {"paid","completed"}]; revenue=sum(o["amount"] for o in paid); upsells=sum(o.get("upsell_added_amount",0) for o in paid if o.get("upsell_accepted"))
    if AUDIT.exists():
        audit_entries=[json.loads(x) for x in AUDIT.read_text(encoding="utf-8").splitlines() if x]
        completed_ids={(e.get("inputs") or {}).get("order_id") for e in audit_entries if (e.get("tool") or e.get("action"))=="check_payment_status" and (e.get("result") or {}).get("status") in {"paid","completed"}}
        for e in audit_entries:
            if (e.get("tool") or e.get("action"))!="accept_upsell":continue
            result=e.get("result") or {}; bundle_id=result.get("order_id")
            if bundle_id not in completed_ids or bundle_id in orders:continue
            inputs=e.get("inputs") or e.get("input") or {}; addon=product(inputs.get("added_product"))
            if addon:upsells+=addon["price"]
    failures=[o for o in values if o.get("status") not in {"paid","completed"}]+list(attempts.values()); breakdown={}
    for item in failures:
        reason=item.get("failure_reason") or "unknown"; breakdown[reason]=breakdown.get(reason,0)+1
    attempted=len(paid)+len(failures); guardrail_blocks=sum(count for reason,count in breakdown.items() if reason.startswith("guardrail_"))
    running=0; series=[]
    for order in sorted(paid,key=lambda o:o.get("completed_at") or o["created_at"]):
        running+=order["amount"]; series.append({"label":(order.get("completed_at") or order["created_at"])[5:16].replace("T"," "),"revenue":running})
    return {"revenue":revenue,"upsell_revenue":upsells,"upsell_percent":round(upsells/revenue*100,1) if revenue else 0,"completed":len(paid),"attempted":attempted,"conversion_rate":round(len(paid)/attempted*100,1) if attempted else 0,"failure_breakdown":breakdown,"guardrail_blocks":guardrail_blocks,"revenue_series":series[-12:]}
@app.get("/api/merchant/config")
def get_config():return limits()
@app.put("/api/merchant/config")
def put_config(value:MerchantConfig):
    data=value.model_dump(); CONFIG.write_text(json.dumps(data,indent=2),encoding="utf-8"); log("update_guardrails",data,{"saved":True},"The merchant changed the boundaries used by every checkout channel."); return data
@app.post("/api/merchant/campaign")
def campaign_check():
    sold={}
    for o in orders.values():
        if o["status"] in {"paid","completed"}:sold[o["product"]["id"]]=sold.get(o["product"]["id"],0)+o["quantity"]
    if sold:
        pid,units=max(sold.items(),key=lambda x:x[1]); p=product(pid); suggestion=f"{p['name']} is the top seller with {units} paid unit(s). Bundle it with Pace Crew Socks to grow basket value."; signal={"type":"top_seller","product_id":pid,"paid_units":units}
    else:
        p=min((x for x in CATALOG if x["stock"]>0),key=lambda x:x["stock"]); suggestion=f"{p['name']} has the lowest available stock ({p['stock']} units). Feature it as limited availability rather than discounting it."; signal={"type":"low_stock","product_id":p["id"],"stock":p["stock"]}
    generator="data-grounded fallback"
    if os.getenv("GEMINI_API_KEY","").strip():
        try:
            prompt=f"You are a merchant growth analyst. Write one concise, specific action (maximum 35 words) based only on this signal: {json.dumps(signal)}. Product catalog: {json.dumps([public(p) for p in CATALOG])}. Do not invent discounts or facts."
            generated=call_gemini(prompt)
            if generated:suggestion=generated; generator="Gemini"
        except Exception:pass
    out={"generated_at":now(),"signal":signal,"suggestion":suggestion,"generator":generator}; CAMPAIGN.write_text(json.dumps(out,indent=2),encoding="utf-8"); log("run_campaign_check",signal,{"suggestion":suggestion,"generator":generator},"Paid-order and stock data identified one actionable growth signal for a grounded campaign recommendation."); return out
@app.get("/api/merchant/campaign")
def get_campaign():return read(CAMPAIGN,None)
@app.post("/api/chat")
def chat(request:ChatRequest):return chat_reply(request.message,request.session_id or "anonymous")
