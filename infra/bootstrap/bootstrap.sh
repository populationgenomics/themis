#!/usr/bin/env bash
#
# One-time-per-environment bootstrap, run locally by an operator with Owner on
# the target project. Never run in CI. Creates only what Pulumi itself needs to
# exist first — the state bucket, the KMS key for the secrets provider, and the
# GitHub WIF identities CI authenticates as — plus baseline network hardening.
# Everything else is the Pulumi program (one `pulumi up`). Idempotent: re-running
# is safe. See docs/runbooks/fresh-environment.md.
#
# Usage: PROJECT=cpg-themis-dev ./bootstrap.sh   (prod: PROJECT=cpg-themis-prod)

set -euo pipefail

PROJECT="${PROJECT:-cpg-themis-dev}"
REGION="${REGION:-australia-southeast1}"
REPO='populationgenomics/themis-internal'

STATE_BUCKET="${PROJECT}-pulumi-state"
KMS_KEYRING='themis'
KMS_KEY='pulumi'
POOL='github'
PROVIDER='github'
DEPLOY_SA="themis-deploy@${PROJECT}.iam.gserviceaccount.com"
PREVIEW_SA="themis-preview@${PROJECT}.iam.gserviceaccount.com"

echo "Bootstrapping ${PROJECT} (region ${REGION})"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"

# --- APIs bootstrap needs (the program enables the rest: run, iap, ...) --------
gcloud services enable --project="${PROJECT}" \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudkms.googleapis.com storage.googleapis.com compute.googleapis.com \
  serviceusage.googleapis.com cloudresourcemanager.googleapis.com

# --- State bucket: private, versioned, per-environment (never shared) ----------
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT}" --location="${REGION}" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning

# --- KMS key for the gcpkms secrets provider ----------------------------------
gcloud kms keyrings create "${KMS_KEYRING}" --project="${PROJECT}" --location="${REGION}" 2>/dev/null || true
gcloud kms keys create "${KMS_KEY}" --project="${PROJECT}" --location="${REGION}" \
  --keyring="${KMS_KEYRING}" --purpose=encryption 2>/dev/null || true

# --- Service accounts: deploy (write, main only) and preview (read-only, PRs) --
gcloud iam service-accounts create themis-deploy --project="${PROJECT}" \
  --display-name='Themis Pulumi deploy (write; push to main)' 2>/dev/null || true
gcloud iam service-accounts create themis-preview --project="${PROJECT}" \
  --display-name='Themis Pulumi preview (read-only; pull requests)' 2>/dev/null || true

# The deploy SA's other build-time roles are program-managed (deploy_iam.py);
# these two can't be: storage.admin reads its own state, projectIamAdmin grants the rest.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/resourcemanager.projectIamAdmin --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/storage.admin --condition=None >/dev/null
# Preview SA: read-only — enough to diff the proposed change, never to mutate.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${PREVIEW_SA}" --role=roles/viewer --condition=None >/dev/null

# State bucket: deploy reads/writes state + locks via project storage.admin above;
# preview reads only (project viewer doesn't include object read).
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --member="serviceAccount:${PREVIEW_SA}" --role=roles/storage.objectViewer >/dev/null

# KMS: deploy encrypts/decrypts; preview decrypts so it can diff secret config
# and state values — the gcpkms secrets manager loads on every op, preview too.
gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
  --project="${PROJECT}" --location="${REGION}" --keyring="${KMS_KEYRING}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter >/dev/null
gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
  --project="${PROJECT}" --location="${REGION}" --keyring="${KMS_KEYRING}" \
  --member="serviceAccount:${PREVIEW_SA}" --role=roles/cloudkms.cryptoKeyDecrypter >/dev/null

# --- GitHub WIF: pool + OIDC provider, pinned to this repo --------------------
gcloud iam workload-identity-pools create "${POOL}" --project="${PROJECT}" \
  --location=global --display-name='GitHub Actions' 2>/dev/null || true
gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
  --project="${PROJECT}" --location=global --workload-identity-pool="${POOL}" \
  --display-name='themis-internal' \
  --issuer-uri='https://token.actions.githubusercontent.com' \
  --attribute-mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.event_name=assertion.event_name' \
  --attribute-condition="assertion.repository == '${REPO}'" 2>/dev/null || true

POOL_PATH="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"
# Deploy SA: only tokens on refs/heads/main (push to main) may impersonate it.
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" --project="${PROJECT}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_PATH}/attribute.ref/refs/heads/main" >/dev/null
# Preview SA: only pull_request-event tokens may impersonate it.
gcloud iam service-accounts add-iam-policy-binding "${PREVIEW_SA}" --project="${PROJECT}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_PATH}/attribute.event_name/pull_request" >/dev/null

# --- Network hardening: drop the auto-created default VPC and its permissive ---
# default-allow-ssh/rdp/icmp/internal rules. The skeleton runs on Cloud Run +
# global LB (no VPC); the sandbox brings its own deny-by-default VPC later.
for rule in default-allow-icmp default-allow-internal default-allow-rdp default-allow-ssh; do
  gcloud compute firewall-rules delete "${rule}" --project="${PROJECT}" --quiet 2>/dev/null || true
done
gcloud compute networks delete default --project="${PROJECT}" --quiet 2>/dev/null || true

cat <<EOF

Bootstrap complete for ${PROJECT}.

Next (first bring-up — see docs/runbooks/fresh-environment.md):
  cd infra
  pulumi login "gs://${STATE_BUCKET}"
  pulumi stack init ${PROJECT##*-}    # 'dev' / 'prod' from the project suffix
  THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi up
  pulumi stack output lb_ip           # hand this IP to IT for the A record
EOF
