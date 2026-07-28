#!/bin/bash
# ext4_feature_upgrade.sh - Audit and upgrade ext4 filesystem features
#
# $Id: ext4_feature_upgrade.sh,v 1.00 2026/07/23 00:00:00 root Exp $
#
# Detects missing ext4 features (64bit, metadata_csum) and reserved
# block percentage drift across hypervisors.  Default mode is audit;
# --upgrade performs the actual conversion (filesystem MUST be
# unmounted first).
#
# VERSION HISTORY:
# v1.01 (2026-07-23): Default reserved-pct=2%, fstab fallback for
#                     unmounted fs, hastop/hastart workflow in help
# v1.00 (2026-07-23): Initial release - audit + upgrade for 64bit,
#                     metadata_csum, reserved-block normalisation

set -euo pipefail

# -------------------------------------------------------------------
# Defaults
# -------------------------------------------------------------------
MODE="audit"
RESERVED_PCT="2"
TARGET_FEATURES="64bit metadata_csum"
FORCE=0
VERBOSE=0

# -------------------------------------------------------------------
# Colours (Ansible-style)
# -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# -------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] <MOUNTPOINT|DEVICE>

Audit or upgrade ext4 feature flags and reserved-block settings.

Modes:
  --audit           Show current state vs. desired (default)
  --upgrade         Perform the actual upgrade (fs must be unmounted)

Options:
  --reserved-pct N  Set reserved-block percentage (default: 2%)
  --force           Skip confirmation prompts
  -v, --verbose     Show detailed tune2fs / resize2fs output
  -h, --help        This help

Target features checked / upgraded:
  64bit             Enable 64-bit block numbers (resize2fs -b)
  metadata_csum     Enable metadata checksums   (tune2fs -O)

Examples:
  # Audit /export/home on this host:
  $(basename "$0") /export/home

  # Audit a specific device:
  $(basename "$0") /dev/mapper/rootdg-lv_home

  # Upgrade (filesystem MUST be unmounted):
  $(basename "$0") --upgrade /dev/mapper/rootdg-lv_home

Typical workflow (Krynn hypervisors):
  1. hastop -local            # stop VCS / HA services
  2. umount /export/home      # unmount the filesystem
  3. $(basename "$0") --upgrade /dev/mapper/rootdg-lv_home
  4. mount /export/home       # re-mount
  5. hastart                  # restart HA services

Notes:
  - The 64bit and metadata_csum upgrades REQUIRE the filesystem to be
    unmounted.  Use hastop + umount first.
  - Reserved-block changes (tune2fs -m) CAN be done on a mounted fs.
  - e2fsck is run automatically before and after feature upgrades.
EOF
    exit 0
}

# -------------------------------------------------------------------
die()  { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }
warn() { echo -e "${YELLOW}WARNING:${NC} $*" >&2; }
info() { echo -e "${GREEN}>>>${NC} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}=== $* ===${NC}"; }

# -------------------------------------------------------------------
# Parse arguments
# -------------------------------------------------------------------
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --audit)        MODE="audit";   shift ;;
        --upgrade)      MODE="upgrade"; shift ;;
        --reserved-pct) RESERVED_PCT="$2"; shift 2 ;;
        --force)        FORCE=1; shift ;;
        -v|--verbose)   VERBOSE=1; shift ;;
        -h|--help)      usage ;;
        -*)             die "Unknown option: $1" ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]:-}"

[[ $# -lt 1 ]] && die "No mountpoint or device specified.  Use -h for help."
TARGET="$1"

# -------------------------------------------------------------------
# Resolve device from mountpoint if needed
# -------------------------------------------------------------------
if [[ -b "$TARGET" ]]; then
    DEV="$TARGET"
    MNTPT=$(findmnt -n -o TARGET "$DEV" 2>/dev/null || true)
else
    # Treat as mountpoint — try mounted fs first, fall back to fstab
    MNTPT="$TARGET"
    DEV=$(findmnt -n -o SOURCE "$MNTPT" 2>/dev/null || true)
    if [[ -z "$DEV" || ! -b "$DEV" ]]; then
        DEV=$(awk -v mp="$MNTPT" '$2 == mp {print $1; exit}' \
            /etc/fstab 2>/dev/null || true)
    fi
    if [[ -z "$DEV" || ! -b "$DEV" ]]; then
        DEV=$(df --output=source "$MNTPT" 2>/dev/null | tail -1) \
            || true
    fi
    [[ -n "$DEV" && -b "$DEV" ]] \
        || die "Cannot resolve block device for $MNTPT" \
               "(not mounted and not in /etc/fstab)"
fi

# -------------------------------------------------------------------
# Collect current filesystem state
# -------------------------------------------------------------------
TUNE2FS_OUT=$(tune2fs -l "$DEV" 2>/dev/null) \
    || die "tune2fs -l failed on $DEV — is this ext4?"

get_field() {
    echo "$TUNE2FS_OUT" | grep "^$1" | sed "s/^$1[[:space:]]*//"
}

CUR_FEATURES=$(get_field "Filesystem features:")
CUR_CREATED=$(get_field "Filesystem created:")
CUR_BLOCK_COUNT=$(get_field "Block count:")
CUR_RESERVED=$(get_field "Reserved block count:")
CUR_FREE=$(get_field "Free blocks:")
CUR_BLOCK_SIZE=$(get_field "Block size:")
CUR_INODE_COUNT=$(get_field "Inode count:")
CUR_FREE_INODES=$(get_field "Free inodes:")
CUR_JOURNAL=$(dumpe2fs -h "$DEV" 2>/dev/null \
    | grep "journal size:" | sed 's/.*:[[:space:]]*//' || echo "unknown")

# Compute current reserved percentage
if [[ -n "$CUR_BLOCK_COUNT" && -n "$CUR_RESERVED" \
   && "$CUR_BLOCK_COUNT" -gt 0 ]]; then
    CUR_RESERVED_PCT=$(awk "BEGIN {
        printf \"%.2f\", $CUR_RESERVED / $CUR_BLOCK_COUNT * 100
    }")
else
    CUR_RESERVED_PCT="N/A"
fi

# Compute sizes in GiB
blocks_to_gib() {
    awk "BEGIN { printf \"%.1f\", $1 * $CUR_BLOCK_SIZE / 1073741824 }"
}
TOTAL_GIB=$(blocks_to_gib "$CUR_BLOCK_COUNT")
FREE_GIB=$(blocks_to_gib "$CUR_FREE")
RESERVED_GIB=$(blocks_to_gib "$CUR_RESERVED")

# Check which target features are missing
MISSING_FEATURES=()
for feat in $TARGET_FEATURES; do
    if ! echo "$CUR_FEATURES" | grep -qw "$feat"; then
        MISSING_FEATURES+=("$feat")
    fi
done

IS_MOUNTED=0
if findmnt "$DEV" &>/dev/null; then
    IS_MOUNTED=1
fi

# -------------------------------------------------------------------
# Audit output
# -------------------------------------------------------------------
hdr "$(hostname): $DEV"
[[ -n "$MNTPT" ]] && echo -e "  Mountpoint:       $MNTPT"
echo -e "  Created:          $CUR_CREATED"
echo -e "  Total size:       ${TOTAL_GIB} GiB  ($CUR_BLOCK_COUNT blocks)"
echo -e "  Free:             ${FREE_GIB} GiB  ($CUR_FREE blocks)"
echo -e "  Reserved:         ${RESERVED_GIB} GiB  ($CUR_RESERVED blocks, ${CUR_RESERVED_PCT}%)"
echo -e "  Journal:          $CUR_JOURNAL"
echo -e "  Inode count:      $CUR_INODE_COUNT  (free: $CUR_FREE_INODES)"
if [[ $IS_MOUNTED -eq 1 ]]; then
    echo -e "  Status:           ${GREEN}mounted${NC}"
else
    echo -e "  Status:           ${YELLOW}unmounted${NC}"
fi

hdr "Feature flags"
echo -e "  Current:  $CUR_FEATURES"
echo ""
for feat in $TARGET_FEATURES; do
    if echo "$CUR_FEATURES" | grep -qw "$feat"; then
        echo -e "  ${GREEN}✓${NC} $feat"
    else
        echo -e "  ${RED}✗${NC} $feat  ${YELLOW}(missing)${NC}"
    fi
done

if [[ -n "$RESERVED_PCT" ]]; then
    echo ""
    echo -e "  Reserved blocks:  current ${CUR_RESERVED_PCT}%," \
            "requested ${RESERVED_PCT}%"
fi

# -------------------------------------------------------------------
# Audit-only: print summary and exit
# -------------------------------------------------------------------
if [[ "$MODE" == "audit" ]]; then
    hdr "Recommendations"
    NEEDS_WORK=0

    if [[ ${#MISSING_FEATURES[@]} -gt 0 ]]; then
        NEEDS_WORK=1
        echo -e "  The following features should be added:"
        for feat in "${MISSING_FEATURES[@]}"; do
            case "$feat" in
                64bit)
                    echo -e "    ${CYAN}64bit${NC}: enables >16 TiB" \
                            "support and modern group descriptors"
                    echo -e "           ${BOLD}Command:${NC}" \
                            "resize2fs -b $DEV"
                    ;;
                metadata_csum)
                    echo -e "    ${CYAN}metadata_csum${NC}: adds" \
                            "crc32c checksums to all metadata"
                    echo -e "           ${BOLD}Command:${NC}" \
                            "tune2fs -O metadata_csum $DEV &&" \
                            "e2fsck -Df $DEV"
                    ;;
            esac
        done
        echo ""
        echo -e "  ${YELLOW}⚠  Filesystem must be unmounted for" \
                "feature upgrades.${NC}"
        echo -e "     1) hastop -local"
        echo -e "     2) umount ${MNTPT:-$DEV}"
        echo -e "     3) ${BOLD}ext4_feature_upgrade.sh --upgrade" \
                "$DEV${NC}"
        echo -e "     4) mount ${MNTPT:-$DEV}"
        echo -e "     5) hastart"
    fi

    if [[ -n "$RESERVED_PCT" ]] \
    && [[ $(awk "BEGIN{print($CUR_RESERVED_PCT!=$RESERVED_PCT)}") == "1" ]]; then
        NEEDS_WORK=1
        echo -e "  Reserved blocks should be changed from" \
                "${CUR_RESERVED_PCT}% to ${RESERVED_PCT}%"
        echo -e "    ${BOLD}Command:${NC} tune2fs -m $RESERVED_PCT $DEV"
        echo -e "    (can be done on a ${GREEN}mounted${NC} filesystem)"
    fi

    if [[ $NEEDS_WORK -eq 0 ]]; then
        echo -e "  ${GREEN}All features present and settings" \
                "nominal — nothing to do.${NC}"
    fi
    exit 0
fi

# -------------------------------------------------------------------
# Upgrade mode
# -------------------------------------------------------------------
hdr "Upgrade mode"

# --- Reserved blocks (online-safe) ---------------------------------
if [[ -n "$RESERVED_PCT" ]] \
&& [[ $(awk "BEGIN{print($CUR_RESERVED_PCT!=$RESERVED_PCT)}") == "1" ]]; then
    info "Setting reserved blocks to ${RESERVED_PCT}% ..."
    tune2fs -m "$RESERVED_PCT" "$DEV"
    info "Reserved blocks updated."
fi

# --- Feature upgrades (require unmount) ----------------------------
if [[ ${#MISSING_FEATURES[@]} -eq 0 ]]; then
    info "All target features already present — nothing to upgrade."
    exit 0
fi

if [[ $IS_MOUNTED -eq 1 ]]; then
    die "Filesystem $DEV is currently mounted at $MNTPT." \
        "\n       Feature upgrades require an unmounted filesystem." \
        "\n       Unmount it first, or boot into rescue mode."
fi

# Confirm unless --force
if [[ $FORCE -eq 0 ]]; then
    echo ""
    echo -e "${YELLOW}About to upgrade features on $DEV:${NC}"
    for feat in "${MISSING_FEATURES[@]}"; do
        echo "  + $feat"
    done
    echo ""
    read -r -p "Continue? [y/N] " REPLY
    [[ "$REPLY" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# Pre-flight fsck
info "Running e2fsck -f $DEV ..."
if [[ $VERBOSE -eq 1 ]]; then
    e2fsck -f -y "$DEV" || true
else
    e2fsck -f -y "$DEV" >/dev/null 2>&1 || true
fi

# Apply each missing feature in order
for feat in "${MISSING_FEATURES[@]}"; do
    case "$feat" in
        64bit)
            info "Enabling 64bit feature (resize2fs -b) ..."
            if [[ $VERBOSE -eq 1 ]]; then
                resize2fs -b "$DEV"
            else
                resize2fs -b "$DEV" 2>&1
            fi
            info "64bit feature enabled."
            ;;
        metadata_csum)
            info "Enabling metadata_csum (tune2fs -O) ..."
            if [[ $VERBOSE -eq 1 ]]; then
                echo y | tune2fs -O metadata_csum "$DEV"
            else
                echo y | tune2fs -O metadata_csum "$DEV" 2>&1
            fi
            info "metadata_csum enabled."
            ;;
    esac
done

# Post-upgrade fsck to rebuild checksums and optimise dirs
info "Running e2fsck -fyD $DEV (rebuild checksums + optimise) ..."
if [[ $VERBOSE -eq 1 ]]; then
    e2fsck -f -y -D "$DEV" || true
else
    e2fsck -f -y -D "$DEV" 2>&1 || true
fi

hdr "Upgrade complete"
info "Verify with:  tune2fs -l $DEV | grep features"
info "Then mount:   mount $DEV $MNTPT"
