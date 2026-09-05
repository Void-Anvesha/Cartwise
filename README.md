# Cartwise — Conversational Checkout Agent

Cartwise is a catalog-grounded shopping assistant with bounded checkout and a visible audit trail. It supports running shoes, trail shoes, socks, and bottles from `catalog.json`.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Add your Razorpay Test API Key and Test Key Secret to .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

Merchant analytics, guardrails, campaign recommendations, and the explainable audit trail are at http://127.0.0.1:8000/merchant.

Cartwise uses one shared dashboard design system across the authenticated storefront and merchant tools: persistent navy navigation, shared cards and indigo actions, and responsive mobile drawers. The revenue hero retains a warm analytics accent. Login and signup reuse the same tokens without authenticated navigation.

## Autonomous buyer

With the server running, launch a UI-independent buyer (Node 20+):

```powershell
node --env-file=.env buyer-agent.js "Buy the cheapest in-stock running shoes, size 9"
```

It reads `GET /api/catalog`, requires a structured Gemini decision, and creates the server-priced order through `POST /api/order`. Check it with `GET /api/order/{id}/status`. The script exits immediately when `GEMINI_API_KEY` is unavailable; it never silently substitutes deterministic purchasing logic.

Without Razorpay credentials, the app uses a safe demo checkout, including a reproducible failed-payment button. With `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`, it creates Razorpay test-mode orders, opens the official Checkout widget, and verifies its payment signature on the server.

## Demo sequence

1. `hi`
2. `show me running shoes under 3000`
3. Choose an item card or say `StrideRun Aero Blue`.
4. Say `give me the payment link` to display checkout.
5. Use **Simulate failure** for the deliberate failure case, or complete the test payment.
6. Say `buy 500 pairs of shoes` to demonstrate the visible quantity guardrail.
7. Say `show me kurtis` to see a catalog-aware off-catalog response.

## Safety and auditability

- Catalog, stock, size, quantity, and order-value rules are enforced on the server and configured from the merchant dashboard.
- Products are selected and confirmed before checkout; the browser only receives the public Razorpay Key ID.
- Duplicate matching orders in the same session are blocked for five minutes.
- `GET /api/catalog` is an AI-readable merchant catalog.
- `GET /api/audit` records actual search, selection, order, payment, and guardrail inputs and outcomes.
