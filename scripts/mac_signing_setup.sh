#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
#
# Prépare le certificat de signature macOS DEPUIS LINUX, sans Mac.
#
# Apple documente la génération d'une demande de certificat avec « Trousseau
# d'accès », qui n'existe que sur macOS. La même demande se produit avec
# openssl, et le certificat qu'Apple renvoie s'assemble en .p12 de la même
# façon. Aucune étape ne requiert de machine Apple.
#
# Les fichiers vivent hors du dépôt : une clé privée de signature n'a rien à
# faire dans un répertoire versionné.
#
# Usage :
#   scripts/mac_signing_setup.sh csr    "Prénom Nom" courriel@exemple.fr
#   scripts/mac_signing_setup.sh p12                       (après téléchargement du .cer)
#   scripts/mac_signing_setup.sh identity                  (affiche l'identité de signature)
#   scripts/mac_signing_setup.sh secrets                   (affiche ce qu'il faut coller dans GitHub)

set -euo pipefail

DOSSIER="${ZLINK_SIGNING_DIR:-$HOME/.zlink-signing}"
CLE="$DOSSIER/developerID.key"
CSR="$DOSSIER/developerID.certSigningRequest"
CER="$DOSSIER/developerID_application.cer"
# Apple nomme le fichier différemment selon le navigateur : on prend le premier
# .cer du dossier si le nom attendu n'y est pas.
if [ ! -f "$CER" ]; then
    _trouve=$(ls "$DOSSIER"/*.cer 2>/dev/null | head -1 || true)
    [ -n "${_trouve:-}" ] && CER="$_trouve"
fi
P12="$DOSSIER/developerID.p12"
INTER="$DOSSIER/DeveloperIDG2CA.cer"

# Autorité intermédiaire d'Apple. codesign a besoin de la CHAÎNE complète :
# un .p12 qui ne contient que la feuille produit un « unable to build chain ».
URL_INTER="https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer"

info() { printf '  %s\n' "$*"; }

cmd_csr() {
  local nom="${1:-}" courriel="${2:-}"
  if [ -z "$nom" ] || [ -z "$courriel" ]; then
    echo "usage : $0 csr \"Prénom Nom\" courriel@exemple.fr" >&2
    exit 2
  fi
  mkdir -p "$DOSSIER"
  chmod 700 "$DOSSIER"
  if [ -f "$CLE" ]; then
    info "Clé déjà présente : $CLE (conservée)"
  else
    # 2048 bits RSA : ce qu'Apple accepte pour un Developer ID.
    openssl genrsa -out "$CLE" 2048 2>/dev/null
    chmod 600 "$CLE"
    info "Clé privée créée : $CLE"
  fi
  openssl req -new -key "$CLE" -out "$CSR" \
    -subj "/emailAddress=$courriel/CN=$nom/C=FR"
  info "Demande de certificat : $CSR"
  echo
  info "À faire maintenant sur developer.apple.com :"
  info "  Certificates, IDs & Profiles → Certificates → +"
  info "  Choisir « Developer ID Application » (section Software)"
  info "  Téléverser $CSR"
  info "  Télécharger le .cer et l'enregistrer sous $CER"
  info "Puis relancer : $0 p12"
}

cmd_p12() {
  [ -f "$CER" ] || { echo "Certificat absent : $CER" >&2; exit 1; }
  [ -f "$CLE" ] || { echo "Clé privée absente : $CLE" >&2; exit 1; }
  curl -fsSL "$URL_INTER" -o "$INTER"
  openssl x509 -inform DER -in "$CER"   -out "$DOSSIER/leaf.pem"
  openssl x509 -inform DER -in "$INTER" -out "$DOSSIER/inter.pem"
  if [ -n "${ZLINK_P12_PASSWORD:-}" ]; then
    MDP="$ZLINK_P12_PASSWORD"
  else
    read -r -s -p "  Mot de passe pour le .p12 : " MDP; echo
  fi
  # -legacy : « security import » de macOS ne lit pas le chiffrement moderne
  # qu'OpenSSL 3 emploie par défaut.
  openssl pkcs12 -export -legacy \
    -inkey "$CLE" -in "$DOSSIER/leaf.pem" -certfile "$DOSSIER/inter.pem" \
    -out "$P12" -passout "pass:$MDP"
  chmod 600 "$P12"
  rm -f "$DOSSIER/leaf.pem" "$DOSSIER/inter.pem"
  info "Paquet créé : $P12"
  info "Mot de passe à mettre dans le secret MAC_CERT_PASSWORD"
}

cmd_identity() {
  [ -f "$CER" ] || { echo "Certificat absent : $CER" >&2; exit 1; }
  # codesign attend exactement le « common name » du certificat.
  openssl x509 -inform DER -in "$CER" -noout -subject \
    | sed -n 's/.*CN *= *\([^,\/]*\).*/\1/p'
}

cmd_secrets() {
  [ -f "$P12" ] || { echo "Paquet absent : $P12 — lancer « $0 p12 »" >&2; exit 1; }
  local sortie="$DOSSIER/secrets-github.txt"
  {
    echo "MAC_SIGN_IDENTITY"
    cmd_identity
    echo
    echo "MAC_CERT_P12"
    base64 -w0 "$P12"; echo
  } > "$sortie"
  chmod 600 "$sortie"
  echo
  info "Secrets écrits dans : $sortie"
  info "Ce fichier contient la clé privée : à supprimer une fois collé dans GitHub."
  echo
  info "Restent à récupérer sur App Store Connect → Utilisateurs et accès →"
  info "Intégrations → Clés API (rôle Developer suffit) :"
  info "  MAC_API_KEY_P8    : base64 -w0 AuthKey_XXXXXXXX.p8"
  info "  MAC_API_KEY_ID    : les 10 caractères du nom de fichier"
  info "  MAC_API_ISSUER_ID : affiché en haut de la page des clés"
}

case "${1:-}" in
  csr)      shift; cmd_csr "$@" ;;
  p12)      cmd_p12 ;;
  identity) cmd_identity ;;
  secrets)  cmd_secrets ;;
  *) sed -n '5,22p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
