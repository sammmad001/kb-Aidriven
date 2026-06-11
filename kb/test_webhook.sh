#!/bin/bash
# Test webhook endpoint
curl -s -X POST http://localhost:8080/webhook/feishu \
  -H "Content-Type: application/json" \
  -d '{"type":"url_verification","challenge":"test123"}'
echo ""
