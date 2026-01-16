"""
PayPal Payment Integration
Handles subscriptions and one-off content purchases via PayPal
"""
import os
import httpx
from typing import Optional, Dict, Any
from decimal import Decimal


class PayPalClient:
    """PayPal API client for handling payments"""

    def __init__(self):
        self.client_id = os.getenv("PAYPAL_CLIENT_ID")
        self.client_secret = os.getenv("PAYPAL_CLIENT_SECRET")
        self.mode = os.getenv("PAYPAL_MODE", "sandbox")  # sandbox or live

        if self.mode == "sandbox":
            self.base_url = "https://api-m.sandbox.paypal.com"
        else:
            self.base_url = "https://api-m.paypal.com"

        self._access_token: Optional[str] = None

    async def get_access_token(self) -> str:
        """Get OAuth 2.0 access token"""
        if self._access_token:
            return self._access_token

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"}
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["access_token"]
            return self._access_token

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Make authenticated request to PayPal API"""
        token = await self.get_access_token()

        request_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if headers:
            request_headers.update(headers)

        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(
                    f"{self.base_url}{endpoint}",
                    headers=request_headers
                )
            elif method == "POST":
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    headers=request_headers,
                    json=data
                )
            elif method == "PATCH":
                response = await client.patch(
                    f"{self.base_url}{endpoint}",
                    headers=request_headers,
                    json=data
                )
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

    async def create_product(self, name: str, description: str) -> str:
        """Create a PayPal product (for subscriptions)"""
        data = {
            "name": name,
            "description": description,
            "type": "SERVICE",
            "category": "SOFTWARE"
        }
        result = await self._make_request("POST", "/v1/catalogs/products", data)
        return result["id"]

    async def create_subscription_plan(
        self,
        product_id: str,
        plan_name: str,
        price: Decimal,
        currency: str = "GBP"
    ) -> str:
        """Create a subscription billing plan"""
        data = {
            "product_id": product_id,
            "name": plan_name,
            "description": f"{plan_name} subscription",
            "billing_cycles": [{
                "frequency": {
                    "interval_unit": "MONTH",
                    "interval_count": 1
                },
                "tenure_type": "REGULAR",
                "sequence": 1,
                "total_cycles": 0,  # Infinite
                "pricing_scheme": {
                    "fixed_price": {
                        "value": str(price),
                        "currency_code": currency
                    }
                }
            }],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "setup_fee_failure_action": "CONTINUE",
                "payment_failure_threshold": 3
            }
        }
        result = await self._make_request("POST", "/v1/billing/plans", data)
        return result["id"]

    async def create_subscription(
        self,
        plan_id: str,
        return_url: str,
        cancel_url: str,
        custom_id: str
    ) -> Dict[str, Any]:
        """Create a subscription (returns approval URL for user)"""
        data = {
            "plan_id": plan_id,
            "custom_id": custom_id,
            "application_context": {
                "brand_name": "FutHub",
                "locale": "en-GB",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url
            }
        }
        result = await self._make_request("POST", "/v1/billing/subscriptions", data)

        # Extract approval URL
        approval_url = None
        for link in result.get("links", []):
            if link["rel"] == "approve":
                approval_url = link["href"]
                break

        return {
            "subscription_id": result["id"],
            "approval_url": approval_url,
            "status": result["status"]
        }

    async def cancel_subscription(self, subscription_id: str, reason: str = "User requested cancellation"):
        """Cancel a subscription"""
        data = {
            "reason": reason
        }
        await self._make_request(
            "POST",
            f"/v1/billing/subscriptions/{subscription_id}/cancel",
            data
        )

    async def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        """Get subscription details"""
        return await self._make_request(
            "GET",
            f"/v1/billing/subscriptions/{subscription_id}"
        )

    async def create_order(
        self,
        amount: Decimal,
        currency: str,
        description: str,
        return_url: str,
        cancel_url: str,
        custom_id: str
    ) -> Dict[str, Any]:
        """Create a one-time payment order"""
        data = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": currency,
                    "value": str(amount)
                },
                "description": description,
                "custom_id": custom_id
            }],
            "application_context": {
                "brand_name": "FutHub",
                "locale": "en-GB",
                "landing_page": "NO_PREFERENCE",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url
            }
        }
        result = await self._make_request("POST", "/v2/checkout/orders", data)

        # Extract approval URL
        approval_url = None
        for link in result.get("links", []):
            if link["rel"] == "approve":
                approval_url = link["href"]
                break

        return {
            "order_id": result["id"],
            "approval_url": approval_url,
            "status": result["status"]
        }

    async def capture_order(self, order_id: str) -> Dict[str, Any]:
        """Capture (complete) a payment order"""
        result = await self._make_request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture"
        )
        return result

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order details"""
        return await self._make_request("GET", f"/v2/checkout/orders/{order_id}")

    async def refund_capture(self, capture_id: str, amount: Optional[Decimal] = None, currency: str = "GBP") -> Dict[str, Any]:
        """Refund a captured payment"""
        data = {}
        if amount:
            data["amount"] = {
                "value": str(amount),
                "currency_code": currency
            }
        return await self._make_request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            data
        )


# Global PayPal client instance
paypal_client = PayPalClient()
