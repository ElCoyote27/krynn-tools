#!/bin/bash
#
# Resolve an image digest reference to a unique tag reference.
#
# Useful for oc-mirror v1 (additionalImages), which does not accept @sha256: refs.
#
# Usage:
#   ./sha256_to_image_url.sh <image@sha256:...>
#   ./sha256_to_image_url.sh docker://registry.redhat.io/rhel8/postgresql-15@sha256:9772f340...
#
# Auth:
#   Uses REGISTRY_AUTH_FILE, or --authfile if set via AUTHFILE env,
#   else ~/.docker/config.json / $XDG_RUNTIME_DIR/containers/auth.json
#
# Output (stdout):
#   registry.example/repo/name:unique-tag
#
# Exit codes:
#   0  resolved
#   1  usage / bad input
#   2  cannot authenticate / pull metadata from registry
#   3  digest ok but no usable unique tag found
#

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
Usage: sha256_to_image_url.sh <image@sha256:digest>

Resolve a digest-pinned image to a unique tag suitable for oc-mirror v1.

Example:
  sha256_to_image_url.sh registry.redhat.io/rhel8/postgresql-15@sha256:9772f340...
  # → registry.redhat.io/rhel8/postgresql-15:1-1789609073
EOF
	exit 1
}

[[ $# -eq 1 ]] || usage
[[ "$1" == "-h" || "$1" == "--help" ]] && usage

RAW="$1"
RAW="${RAW#docker://}"

if [[ "$RAW" != *@sha256:* ]]; then
	echo "ERROR: input must be an image digest reference (repo@sha256:...)" >&2
	echo "Got: $RAW" >&2
	exit 1
fi

REPO="${RAW%@sha256:*}"
DIGEST="sha256:${RAW##*@sha256:}"
REGISTRY="${REPO%%/*}"

# --- authfile selection ---
AUTHFILE="${AUTHFILE:-${REGISTRY_AUTH_FILE:-}}"
if [[ -z "$AUTHFILE" ]]; then
	if [[ -f "${HOME}/.docker/config.json" ]]; then
		AUTHFILE="${HOME}/.docker/config.json"
	elif [[ -n "${XDG_RUNTIME_DIR:-}" && -f "${XDG_RUNTIME_DIR}/containers/auth.json" ]]; then
		AUTHFILE="${XDG_RUNTIME_DIR}/containers/auth.json"
	fi
fi

SKOPEO_AUTH=()
if [[ -n "$AUTHFILE" ]]; then
	SKOPEO_AUTH=(--authfile "$AUTHFILE")
fi

have_registry_auth() {
	local reg="$1"
	local cfg="${AUTHFILE:-}"
	[[ -n "$cfg" && -f "$cfg" ]] || return 1
	# Match exact host or host:port key in auths
	python3 - "$cfg" "$reg" <<'PY'
import json, sys
cfg, reg = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(cfg))
except Exception:
    sys.exit(1)
auths = data.get("auths") or {}
# also support credHelpers / credsStore presence as soft yes for known helpers
if reg in auths and (auths[reg].get("auth") or auths[reg].get("identitytoken")):
    sys.exit(0)
# strip docker:// leftovers / www
candidates = {reg, reg.split(":")[0]}
for k in list(auths):
    if k in candidates or k.endswith("/" + reg) or reg.endswith(k):
        if auths[k].get("auth") or auths[k].get("identitytoken"):
            sys.exit(0)
sys.exit(1)
PY
}

if ! have_registry_auth "$REGISTRY"; then
	echo "WARNING: no explicit auth entry for registry '${REGISTRY}' in ${AUTHFILE:-<none>}." >&2
	echo "WARNING: will still try skopeo (anonymous / helper may work)." >&2
else
	echo "INFO: found credentials for '${REGISTRY}' in ${AUTHFILE}" >&2
fi

if ! command -v skopeo >/dev/null 2>&1; then
	echo "ERROR: skopeo is required but not found in PATH" >&2
	exit 2
fi

SRC="docker://${REPO}@${DIGEST}"
echo "INFO: inspecting ${SRC}" >&2

TMPJSON=$(mktemp)
trap 'rm -f "$TMPJSON"' EXIT

if ! skopeo inspect "${SKOPEO_AUTH[@]}" "$SRC" >"$TMPJSON" 2>/tmp/sha256_to_image_url.err; then
	echo "ERROR: cannot inspect image (auth or network failure):" >&2
	sed 's/^/  /' /tmp/sha256_to_image_url.err >&2 || true
	exit 2
fi

TAG=$(DIGEST="$DIGEST" python3 - "$TMPJSON" <<'PY'
import json, os, re, sys

path = sys.argv[1]
digest = os.environ["DIGEST"]
with open(path) as f:
    data = json.load(f)

got = data.get("Digest") or ""
if got and got != digest:
    print(f"WARNING: inspect Digest {got} != requested {digest}", file=sys.stderr)

labels = data.get("Labels") or {}
repo_tags = data.get("RepoTags") or []

version = (labels.get("version") or labels.get("org.opencontainers.image.version") or "").strip()
release = (labels.get("release") or "").strip()

def is_floating(tag: str) -> bool:
    if tag in ("latest", "stable", "stream", "prod"):
        return True
    # short floaters like "1", "15", "v4.18"
    if re.fullmatch(r"\d+", tag):
        return True
    if re.fullmatch(r"v?\d+\.\d+", tag):
        return True
    return False

# 1) RHEL-style version-release from labels (preferred for additionalImages).
#    Trust labels even if RepoTags is huge/incomplete in the inspect payload.
if version and release:
    vr = f"{version}-{release}"
    print(f"INFO: selected tag '{vr}' (labels version-release)", file=sys.stderr)
    print(vr)
    sys.exit(0)

candidates = []

# 2) Unique version-epoch tags present on this digest (e.g. 1-1789609073)
epochish = [t for t in repo_tags if re.fullmatch(r"\d+-\d+", t)]
if epochish:
    if release:
        for t in epochish:
            if t.endswith("-" + release) or (version and t == f"{version}-{release}"):
                candidates.append(("2-epoch-label", t))
                break
    best = sorted(epochish, key=lambda t: int(t.split("-", 1)[1]))[-1]
    candidates.append(("2-epoch", best))

# 3) Other non-floating tags (skip -source)
other = [
    t for t in repo_tags
    if not is_floating(t) and not t.endswith("-source") and "sha256" not in t
]
other_sorted = sorted(other, key=lambda t: (len(t), t), reverse=True)
for t in other_sorted[:5]:
    candidates.append(("3-other", t))

# 4) Last resort: floating tag
for t in repo_tags:
    if is_floating(t):
        candidates.append(("4-floating", t))

if not candidates:
    print("ERROR: no RepoTags/labels available to derive a unique tag", file=sys.stderr)
    sys.exit(3)

kind, tag = candidates[0]
print(f"INFO: selected tag '{tag}' ({kind})", file=sys.stderr)
print(tag)
PY
) || {
	echo "ERROR: failed to select a unique tag for ${DIGEST}" >&2
	exit 3
}

echo "${REPO}:${TAG}"
