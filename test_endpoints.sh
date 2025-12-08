#!/bin/bash
# Endpoint Testing Script
# Run this on your deployed server to test the new endpoints

echo "🔍 Testing Backend Endpoints..."
echo ""

# Get your server URL (change this to your actual URL)
BASE_URL="${BASE_URL:-http://localhost:8000}"

echo "Base URL: $BASE_URL"
echo ""

# Test 1: AI Copilot
echo "1️⃣ Testing POST /api/ai/copilot"
curl -X POST "$BASE_URL/api/ai/copilot" \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello"}' \
  -w "\nStatus: %{http_code}\n" \
  -s | head -20
echo ""
echo "---"

# Test 2: Portfolio Optimizer
echo "2️⃣ Testing POST /api/ai/optimize-portfolio"
curl -X POST "$BASE_URL/api/ai/optimize-portfolio" \
  -H "Content-Type: application/json" \
  -d '{"goal":"balanced"}' \
  -w "\nStatus: %{http_code}\n" \
  -s | head -20
echo ""
echo "---"

# Test 3: Market Sentiment
echo "3️⃣ Testing GET /api/market/sentiment"
curl -X GET "$BASE_URL/api/market/sentiment?timeframe=24h" \
  -w "\nStatus: %{http_code}\n" \
  -s | head -20
echo ""
echo "---"

# Test 4: Bulk Trades (will fail without auth, but should return 401 not 404)
echo "4️⃣ Testing POST /api/trades/bulk"
curl -X POST "$BASE_URL/api/trades/bulk" \
  -H "Content-Type: application/json" \
  -d '{"trades":[]}' \
  -w "\nStatus: %{http_code}\n" \
  -s | head -20
echo ""
echo "---"

echo ""
echo "📝 Status Code Guide:"
echo "  200-299 = Success ✅"
echo "  401 = Unauthorized (endpoint exists but needs auth) ✅"
echo "  404 = Not Found (endpoint doesn't exist) ❌"
echo "  500 = Server Error (endpoint exists but has a bug) ⚠️"
