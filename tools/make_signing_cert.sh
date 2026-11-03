#!/bin/bash
# One-time: create the code-signing identity "Macpurge Build" in its OWN
# keychain (~/Library/Keychains/macpurge-signing.keychain-db) with a random
# password stored in ~/.macpurge/signing-keychain-pass (chmod 600).
# The build script unlocks that keychain itself, so codesign never needs the
# login keychain password and no "codesign wants to access key" dialogs appear.
# Same identity on every build = stable app identity = Full Disk Access survives.
set -euo pipefail
NAME="Macpurge Build"
KC="$HOME/Library/Keychains/macpurge-signing.keychain-db"
PASSFILE="$HOME/.macpurge/signing-keychain-pass"

if [[ -f "$KC" && -f "$PASSFILE" ]]; then
  echo "Signing keychain already exists. Nothing to do."; exit 0
fi
mkdir -p "$(dirname "$PASSFILE")"
KCPASS=$(openssl rand -hex 24)
( umask 077; printf '%s' "$KCPASS" > "$PASSFILE" )

security create-keychain -p "$KCPASS" "$KC"
security set-keychain-settings "$KC"            # no auto-lock timeout
security unlock-keychain -p "$KCPASS" "$KC"

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
security import "$D/id.p12" -k "$KC" -P "$P" -T /usr/bin/codesign
# Let Apple tools (codesign) use the key without a dialog.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPASS" "$KC" >/dev/null

# Add to the keychain search list (keeping the existing ones).
EXISTING=$(security list-keychains -d user | sed 's/[" ]//g')
security list-keychains -d user -s $EXISTING "$KC"
echo "Created \"$NAME\" in its own keychain. Builds now sign without dialogs."
