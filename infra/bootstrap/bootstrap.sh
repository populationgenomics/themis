#!/usr/bin/env bash
#
# One-time-per-environment bootstrap, run locally by an operator with Owner on
# the target project. Never run in CI. Creates only what Pulumi itself needs to
# exist first — the state bucket, the KMS key for the secrets provider, and the
# GitHub WIF identities CI authenticates as — plus baseline network hardening.
# Everything else is the Pulumi program (one `pulumi up`). Idempotent: re-running
# is safe. See docs/runbooks/fresh-environment.md.
#
# Usage: PROJECT=<project> ./bootstrap.sh   (e.g. PROJECT=cpg-themis-prod)

set -euo pipefail

# No default: a silent default is how a dev bootstrap lands on prod, or the reverse.
if [[ -z "${PROJECT:-}" ]]; then
  echo "Usage: PROJECT=<project> $0   (e.g. PROJECT=cpg-themis-prod)" >&2
  exit 1
fi
REGION="${REGION:-australia-southeast1}"
REPO='populationgenomics/themis-internal'
# The only workflow whose runs may impersonate the deploy SA, matched on the
# OIDC token's `workflow_ref` (`<repo>/<path>@<ref>`), so other workflows that
# run on `main` are refused.
DEPLOY_WORKFLOW="${REPO}/.github/workflows/deploy.yml"
# Refs the deploy workflow may be read from. `main` carries reviewed code. The
# ad-hoc branch is opt-in per environment and its whole gate is the GitHub
# ruleset over it, so adding this member before that ruleset exists hands the
# deploy SA to anyone with repo write — see docs/runbooks/fresh-environment.md,
# which orders the two.
DEPLOY_REFS=("refs/heads/main")
if [[ -n "${ADHOC_DEPLOY_BRANCH:-}" ]]; then
  # The ruleset gating this member targets `deployed/*`, which does not cross a
  # `/`. A value outside that shape binds a ref nothing restricts, and the grant
  # succeeds whether or not the branch exists — so reject it here rather than
  # leave a credential keyed to an ungated ref.
  if [[ ! "${ADHOC_DEPLOY_BRANCH}" =~ ^deployed/[A-Za-z0-9._-]+$ ]]; then
    echo "ADHOC_DEPLOY_BRANCH must match deployed/<env>; got '${ADHOC_DEPLOY_BRANCH}'" >&2
    exit 1
  fi
  DEPLOY_REFS+=("refs/heads/${ADHOC_DEPLOY_BRANCH}")
fi

STATE_BUCKET="${PROJECT}-pulumi-state"
KMS_KEYRING='themis'
KMS_KEY='pulumi'
POOL='github'
PROVIDER='github'
DEPLOY_SA="themis-deploy@${PROJECT}.iam.gserviceaccount.com"
PREVIEW_SA="themis-preview@${PROJECT}.iam.gserviceaccount.com"

# Runs a gcloud command, treating only the named benign outcome as success, so a
# re-run passes over what an earlier run did while any other failure stops it.
tolerating() {
  local pattern="$1" err
  shift
  if ! err="$("$@" 2>&1 >/dev/null)"; then
    if grep -qiE "${pattern}" <<< "${err}"; then
      return 0
    fi
    echo "${err}" >&2
    return 1
  fi
}
create_if_absent() { tolerating 'ALREADY_EXISTS|already exists' "$@"; }
delete_if_present() { tolerating 'NOT_FOUND|not found' "$@"; }

echo "Bootstrapping ${PROJECT} (region ${REGION})"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"

# --- APIs bootstrap needs (the program enables the rest: run, iap, ...) --------
gcloud services enable --project="${PROJECT}" \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudkms.googleapis.com storage.googleapis.com compute.googleapis.com \
  serviceusage.googleapis.com cloudresourcemanager.googleapis.com run.googleapis.com

# --- State bucket: private, versioned, per-environment (never shared) ----------
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT}" --location="${REGION}" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning

# --- KMS key for the gcpkms secrets provider ----------------------------------
create_if_absent gcloud kms keyrings create "${KMS_KEYRING}" --project="${PROJECT}" --location="${REGION}"
create_if_absent gcloud kms keys create "${KMS_KEY}" --project="${PROJECT}" --location="${REGION}" \
  --keyring="${KMS_KEYRING}" --purpose=encryption

# --- Service accounts: deploy (write, the deploy workflow) and preview (read-only, PRs) --
create_if_absent gcloud iam service-accounts create themis-deploy --project="${PROJECT}" \
  --display-name='Themis Pulumi deploy (write; the deploy workflow)'
create_if_absent gcloud iam service-accounts create themis-preview --project="${PROJECT}" \
  --display-name='Themis Pulumi preview (read-only; pull requests)'

# The deploy SA's other build-time roles are program-managed (themis_infra/grants.py, DeployAccountBuilder);
# these two can't be: storage.admin reads its own state, projectIamAdmin grants the rest.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/resourcemanager.projectIamAdmin --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" --role=roles/storage.admin --condition=None >/dev/null
# Preview SA: only what `pulumi preview` reads while the program runs — the
# project number and each service's and job's live image (the key ring lookup is
# granted on the ring below). roles/viewer would also read the project's logs.
# `update` after `create` converges an existing role on exactly this set.
PREVIEW_ROLE='themisPulumiPreviewLookups'
PREVIEW_PERMISSIONS='resourcemanager.projects.get,run.services.get,run.jobs.get'
create_if_absent gcloud iam roles create "${PREVIEW_ROLE}" --project="${PROJECT}" \
  --title='Themis Pulumi preview lookups' --stage=GA --permissions="${PREVIEW_PERMISSIONS}"
gcloud iam roles update "${PREVIEW_ROLE}" --project="${PROJECT}" \
  --permissions="${PREVIEW_PERMISSIONS}" --quiet >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${PREVIEW_SA}" --role="projects/${PROJECT}/roles/${PREVIEW_ROLE}" \
  --condition=None >/dev/null
delete_if_present gcloud projects remove-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${PREVIEW_SA}" --role=roles/viewer --condition=None

# State bucket: deploy reads/writes state + locks via project storage.admin above;
# preview reads only.
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

# Keyring read: the program looks the ring up to place the session-token signing
# key in it, and a cryptoKey-level role does not carry cloudkms.keyRings.get.
# Both SAs evaluate the program, so both need it.
for SA in "${DEPLOY_SA}" "${PREVIEW_SA}"; do
  gcloud kms keyrings add-iam-policy-binding "${KMS_KEYRING}" \
    --project="${PROJECT}" --location="${REGION}" \
    --member="serviceAccount:${SA}" --role=roles/cloudkms.viewer >/dev/null
done

# --- GitHub WIF: pool + OIDC provider, pinned to this repo --------------------
create_if_absent gcloud iam workload-identity-pools create "${POOL}" --project="${PROJECT}" \
  --location=global --display-name='GitHub Actions'
ATTRIBUTE_MAPPING='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.event_name=assertion.event_name,attribute.workflow_ref=assertion.workflow_ref'
ATTRIBUTE_CONDITION="assertion.repository == '${REPO}'"
create_if_absent gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
  --project="${PROJECT}" --location=global --workload-identity-pool="${POOL}" \
  --display-name='themis-internal' \
  --issuer-uri='https://token.actions.githubusercontent.com' \
  --attribute-mapping="${ATTRIBUTE_MAPPING}" \
  --attribute-condition="${ATTRIBUTE_CONDITION}"

POOL_PATH="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"
# Deploy SA: only the deploy workflow, read from a deployable ref, may
# impersonate it. Only adds these members, so a re-run without
# ADHOC_DEPLOY_BRANCH never cuts off a deploy path in use; withdrawing one is a
# manual step (docs/runbooks/fresh-environment.md). Added before the mapping
# update below, so an environment moving off branch-scoped members never sits
# with neither kind matching.
for REF in "${DEPLOY_REFS[@]}"; do
  gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" --project="${PROJECT}" \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/${POOL_PATH}/attribute.workflow_ref/${DEPLOY_WORKFLOW}@${REF}" >/dev/null
done
# Preview SA: only pull_request-event tokens may impersonate it.
gcloud iam service-accounts add-iam-policy-binding "${PREVIEW_SA}" --project="${PROJECT}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_PATH}/attribute.event_name/pull_request" >/dev/null
# The third account bound to this pool, the CI telemetry writer any run of the repo may take,
# is not needed before Pulumi runs: themis_infra/ci_telemetry.py creates and binds it.

# create-oidc no-ops on an existing provider, so the mapping and condition are
# re-applied here — otherwise an edit to either never reaches an environment
# bootstrapped before it, silently.
gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
  --project="${PROJECT}" --location=global --workload-identity-pool="${POOL}" \
  --attribute-mapping="${ATTRIBUTE_MAPPING}" \
  --attribute-condition="${ATTRIBUTE_CONDITION}" >/dev/null

# Branch-scoped deploy members admit every workflow run on that branch and are
# never wanted, so unlike the workflow_ref members above they are removed.
DEPLOY_SA_MEMBERS="$(gcloud iam service-accounts get-iam-policy "${DEPLOY_SA}" --project="${PROJECT}" \
  --flatten='bindings[].members' --filter='bindings.role=roles/iam.workloadIdentityUser' \
  --format='value(bindings.members)')"
while IFS= read -r MEMBER; do
  [[ "${MEMBER}" == */attribute.ref/* ]] || continue
  delete_if_present gcloud iam service-accounts remove-iam-policy-binding "${DEPLOY_SA}" --project="${PROJECT}" \
    --role=roles/iam.workloadIdentityUser --member="${MEMBER}"
done <<< "${DEPLOY_SA_MEMBERS}"

# --- Network hardening: drop the auto-created default VPC and its permissive ---
# default-allow-ssh/rdp/icmp/internal rules. The skeleton runs on Cloud Run +
# global LB (no VPC); the sandbox brings its own deny-by-default VPC later.
for rule in default-allow-icmp default-allow-internal default-allow-rdp default-allow-ssh; do
  delete_if_present gcloud compute firewall-rules delete "${rule}" --project="${PROJECT}" --quiet
done
delete_if_present gcloud compute networks delete default --project="${PROJECT}" --quiet
# A fresh project can create the default network after the deletes above ran.
if [[ -n "$(gcloud compute networks list --project="${PROJECT}" --filter=name=default --format='value(name)')" ]]; then
  echo "The default network still exists in ${PROJECT}; re-run bootstrap.sh to delete it." >&2
  exit 1
fi

cat <<EOF

Bootstrap complete for ${PROJECT}.

Next (first bring-up — see docs/runbooks/fresh-environment.md):
  cd infra
  pulumi login "gs://${STATE_BUCKET}"
  pulumi stack init ${PROJECT##*-}    # 'dev' / 'prod' from the project suffix
  THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi up
  pulumi stack output lb_ip           # hand this IP to IT for the A record
EOF
