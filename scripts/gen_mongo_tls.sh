#!/usr/bin/env bash
# Generate a throwaway CA + server certificate for MongoDB TLS (DEV/STAGING).
# Production: use certs from your real PKI (Vault PKI, ACM/private CA, etc.)
set -euo pipefail
OUT_DIR="${1:-./mongo-tls}"
DAYS="${2:-825}"
HOSTS="mongo,localhost,mongo.internal"

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

echo "==> CA"
openssl genrsa -out ca.key 4096 2>/dev/null
openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
  -subj "/CN=recon-mongo-dev-ca" -out ca.crt

echo "==> server cert (${HOSTS})"
openssl genrsa -out server.key 4096 2>/dev/null
cat > server.cnf <<EOF
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
CN = mongo
[ext]
subjectAltName = DNS:mongo,DNS:localhost,DNS:mongo.internal,IP:127.0.0.1
EOF
openssl req -new -key server.key -out server.csr -config server.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days "$DAYS" -sha256 -extensions ext -extfile server.cnf -out server.crt 2>/dev/null

# mongod expects a single PEM containing key + cert chain
cat server.crt server.key > mongo.pem
cp ca.crt ca.pem

chmod 600 mongo.pem ca.key server.key
echo "done: $OUT_DIR/{ca.pem, mongo.pem}"
echo "mount into the mongo container and run with --tlsMode requireTLS"
