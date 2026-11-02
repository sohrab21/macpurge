#!/bin/bash
# One-time: create a self-signed code-signing identity "Macpurge Self-Signed"
# in the login keychain. Signing every build with the same identity keeps the
# app's identity stable, so macOS keeps Full Disk Access across rebuilds
# (ad-hoc signatures change with every build and lose it).
set -euo pipefail
NAME="Macpurge Self-Signed"
if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  echo "\"$NAME\" already exists in your keychain. Nothing to do."; exit 0
fi
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
cat > "$D/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$D/key.pem" -out "$D/cert.pem" \
  -days 3650 -config "$D/cert.cnf" 2>/dev/null
P=$(openssl rand -hex 12)
openssl pkcs12 -export -legacy -inkey "$D/key.pem" -in "$D/cert.pem" -out "$D/id.p12" \
  -passout pass:"$P" 2>/dev/null \
  || openssl pkcs12 -export -inkey "$D/key.pem" -in "$D/cert.pem" -out "$D/id.p12" -passout pass:"$P"
security import "$D/id.p12" -k ~/Library/Keychains/login.keychain-db -P "$P" -T /usr/bin/codesign
echo "Created \"$NAME\". Now run: bash tools/build_app.sh"
