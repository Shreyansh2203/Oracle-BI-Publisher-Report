import httpx
import asyncio
import json

payload = {
  "customer_name": "New Horizon Foods",
  "payment_reference": None,
  "payment_date": "2026/05/10",
  "invoices": [
    {
      "invoice_number": "126129803472",
      "invoice_date": "2026/10/05",
      "invoice_amount": 3424,
      "description": None,
      "customer_invoice_number": None,
      "storeNo": None
    },
    {
      "invoice_number": "126129803232",
      "invoice_date": "2026/10/05",
      "invoice_amount": 100,
      "description": None,
      "customer_invoice_number": "12612980",
      "storeNo": None
    },
    {
      "invoice_number": "6129803473",
      "invoice_date": "2026/10/05",
      "invoice_amount": 2133,
      "description": None,
      "customer_invoice_number": None,
      "storeNo": None
    }
  ],
  "total_amount": 2300,
  "confidence_score": 0.83,
  "confidence_label": "HIGH",
  "invoice_count": 1,
  "_meta": {
    "file_kind": "native_pdf",
    "filename": "144.98 W.pdf",
    "input_tokens": 5662,
    "output_tokens": 128,
    "total_tokens": 5790,
    "api_calls": 2,
    "response_time_ms": 2488,
    "num_pages": 1,
    "warnings": []
  }
}

async def main():
    from fastapi.testclient import TestClient
    import sys
    sys.path.append("c:\\Oracle-BI-Publisher-Report\\src")
    from bip_api.main import app
    with TestClient(app) as client:
        response = client.post("/reports/match", json=payload)
        print(json.dumps(response.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(main())
