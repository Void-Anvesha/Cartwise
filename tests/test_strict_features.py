import json

import app.main as merchant


def isolate_storage(monkeypatch, tmp_path):
    for name, filename in {
        "AUDIT": "audit.jsonl", "ORDERS": "orders.json", "ATTEMPTS": "attempts.json",
        "CONFIG": "config.json", "CAMPAIGN": "campaign.json",
        "NEGOTIATIONS": "negotiations.json", "AGENTS": "agents.json",
    }.items():
        monkeypatch.setattr(merchant, name, tmp_path / filename)
    monkeypatch.setattr(merchant, "orders", {})
    monkeypatch.setattr(merchant, "attempts", {})
    monkeypatch.setattr(merchant, "negotiations", {})
    monkeypatch.setattr(merchant, "agents", {})
    monkeypatch.setenv("CARTWISE_DEMO_MODE", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "")


def test_negotiation_uses_live_merchant_limit_and_creates_audited_order(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)
    merchant.CONFIG.write_text(json.dumps({
        "max_order_value": 10_000, "max_quantity": 5,
        "allow_negotiation": True, "max_auto_discount_percent": 5,
    }))
    identity = {"agent_id": "verified-test-agent", "agent_name": "Test", "declared_purpose": "Verification"}
    merchant.agents[identity["agent_id"]] = {**identity, "first_seen": merchant.now(), "order_count": 3, "total_spent": 0}
    result = merchant.negotiate(merchant.NegotiationRequest(
        product_id="run-blue-9", requested_price=2500, quantity=1,
        reasoning="The buyer has a firm ₹2,500 budget.", agent_identity=identity,
    ))
    assert result["decision"] == "counter_offered"
    assert result["counter_price"] == round(2799 * .95)
    order = merchant.create_checkout_order(
        "run-blue-9", 1, size=9, agreed_price=result["counter_price"],
        negotiation_id=result["negotiation_id"], agent_identity=identity,
    )
    assert order["ok"] and order["order"]["amount"] == result["counter_price"]
    entries = [json.loads(line) for line in merchant.AUDIT.read_text(encoding="utf-8").splitlines()]
    negotiation = next(entry for entry in entries if entry["tool"] == "negotiate")
    assert "10.7%" in negotiation["reasoning"] and "5%" in negotiation["reasoning"]


def test_campaign_rereads_orders_and_changes_top_seller(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)
    first = {"a": {"id": "a", "status": "completed", "quantity": 2, "product": {"id": "run-blue-9"}}}
    merchant.ORDERS.write_text(json.dumps(first), encoding="utf-8")
    initial = merchant.campaign_check()
    second = {**first, "b": {"id": "b", "status": "completed", "quantity": 3, "product": {"id": "trail-green"}}}
    merchant.ORDERS.write_text(json.dumps(second), encoding="utf-8")
    refreshed = merchant.campaign_check()
    assert initial["signal"]["product_id"] == "run-blue-9"
    assert refreshed["signal"]["product_id"] == "trail-green"
    assert initial["suggestion"] != refreshed["suggestion"]
    assert refreshed["evidence"]["completed_order_count"] == 2


def test_reset_requires_confirmation_and_preserves_config(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)
    merchant.CONFIG.write_text(json.dumps({"max_order_value": 4321, "max_quantity": 4}), encoding="utf-8")
    merchant.orders["demo"] = {"id": "demo"}
    try:
        merchant.reset_merchant_data(merchant.ResetMerchantDataRequest(confirmation="wrong"))
        assert False, "reset must reject an incorrect confirmation"
    except merchant.HTTPException as error:
        assert error.status_code == 400
    result = merchant.reset_merchant_data(merchant.ResetMerchantDataRequest(confirmation="RESET"))
    assert result["ok"] and merchant.orders == {}
    assert merchant.limits()["max_order_value"] == 4321


def test_budget_query_is_not_mistaken_for_greeting(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)
    result = merchant.chat_reply("show me shoes under 500", "budget-test")
    assert result["products"] == []
    assert "no in-stock items" in result["reply"].lower()
    assert "500" in result["reply"]


def test_budget_query_returns_qualifying_products(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)
    result = merchant.chat_reply("show me shoes under 5000", "budget-products-test")
    assert result["products"]
    assert all(product["stock"] > 0 and product["price"] <= 5000 and product["category"] in {"running shoes", "trail shoes"} for product in result["products"])
