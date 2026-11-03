#!/bin/bash

# Enhanced ODF/Ceph Disk Wiping Tool
# 
# VERSION HISTORY:
# v1.16 (2026/11/03) - Verification output and exit status. The SHA-256 of an
#                all-zero window is the same on every line, so it is not printed.
#                nonzero= and compare= are the result. A failed verification
#                exits 1 after the remaining disks on that node are reported.
# v1.15 (2026/11/03) - Container OSDs and a real partition-table check. pgrep as
#                the core user misses ceph-osd on hidepid RHCOS, and pkill -9
#                does not let the OSD flush and close the disk. Stop kubelet
#                only when a rook-ceph-osd pod exists, crictl stop --timeout 30
#                so the process gets SIGTERM, then SIGKILL only what is still
#                alive. After the wipe, flush the page cache and the device
#                cache, then read the first 34 sectors and the last 34 sectors
#                twice: once through the page cache and once with direct I/O.
#                Direct I/O alone cannot see dirty cache, so the check passes
#                only when both reads are zero and identical. The device read
#                uses a root-owned temp file: on RHCOS sudo cannot write a
#                file the core user created in /tmp.
# v1.14 (2026/11/03) - Leftover GPT after a "successful" wipe. v1.13 already ran
#                sgdisk -Z and wipefs -fa, but sgdisk only ran once, at the start,
#                with its errors discarded, and the closing pass was wipefs alone.
#                wipefs stops seeing a GPT once the primary header is gone, so the
#                backup header in the last sector (also easy to miss with a 4K dd)
#                stayed, and the kernel could write it back. Each disk now gets two
#                passes of wipefs -fa then sgdisk --zap-all, the second one after
#                the zeroing. Each pass drops the kernel partition table and flushes.
#                The last 34 sectors are zeroed at 512-byte size. A third pass runs
#                only when wipefs -n still reports a signature.
# v1.13 (2026/10/29) - Stop containerized ceph-osd before the wipe. The CRI container
#                name is "osd" (pod rook-ceph-osd-*), so crictl --name ceph-osd matched
#                nothing and kubelet recreated the process after pkill -9, rewriting
#                the bluestore label. Stop kubelet, then stop those pods with crictl,
#                then SIGKILL only leftovers. If crictl is not installed, skip that
#                path with no message and fall back to pkill.
# v1.12 (2026/09/11) - Bugfix: removed blkdiscard entirely — on NVMe drives that don't
#                guarantee zero-on-read after TRIM (e.g. Dell CM7), blkdiscard was causing
#                the controller to return stale Ceph bluestore metadata for deallocated
#                blocks, undoing the dd zero writes. Added final wipefs pass after dd for
#                belt-and-suspenders. Added pre-wipe ceph-osd container/process kill via
#                crictl+pkill, added post-wipe verification pass that checks each wiped
#                disk for remaining Ceph/bluestore/filesystem signatures
# v1.11 (2026/07/02) - Robustness: added is_valid_disk_name() validation to prevent SSH warning
#                messages (e.g. "Warning: Permanently added...") from being mistaken as device
#                names (/dev/Warning, /dev/Permanently), removed redundant ssh-keygen -R
#                (unnecessary with UserKnownHostsFile=/dev/null and caused repeated warnings),
#                added block device existence verification before wipe operations, improved
#                numeric validation for disk sector counts, added carriage-return stripping
#                to discovery filter
# v1.10 (2025/11/05) - SSH error handling: added connectivity validation before node processing,
#                explicit error reporting when SSH login fails, exit code checking for critical
#                disk discovery commands to prevent silent failures; Added ANSI color support
#                (Ansible-style) for improved output readability with red for errors, green for
#                success/OK status, yellow for warnings/changes; Added -h short option for help
# v1.09 (2025/10/29) - SSH host key handling: added StrictHostKeyChecking=no and UserKnownHostsFile=/dev/null
#                to prevent any prompts about unknown or changed host keys, ensuring fully automated
#                execution even with frequently rebuilt or ephemeral OCP/RHCOS nodes
# v1.08 (2025/10/17) - SSH compatibility fix: removed -qt flags from commands that capture output,
#                fixed BatchMode+pseudo-terminal conflicts causing silent failures, moved pipe filters
#                to local execution, resolved OSTree subpath handling in findmnt output
# v1.07 (2025/10/17) - RHCOS rootdisk detection overhaul: multi-method detection with 5 fallback strategies
#                to handle RHCOS 4.16-4.18+ including ephemeral/Assisted Installer pre-install states,
#                improved compatibility across different RHCOS deployment scenarios
# v1.06 (2025/09/17) - LUKS handling restructure: separated LUKS cleanup into dedicated loop that runs
#                before disk wiping, improved ODF/OCS deviceset detection with deduplication
# v1.05 (2025/09/17) - Disk size validation: skip GB offsets beyond disk capacity, consolidated
#                end-of-disk wiping into main loop, intelligent offset filtering for small disks
# v1.04 (2025/09/17) - LUKS/crypt handling optimization: efficient per-node LUKS discovery,
#                improved ODF/OCS deviceset detection, better crypt mapping management,
#                enhanced debug output for encrypted devices
# v1.03 (2025/09/17) - LUKS encryption support: automatic LUKS/crypt device detection,
#                cryptsetup integration with proper cleanup, ODF-aware encrypted mapping
#                handling, seamless integration with live vs simulation modes
# v1.02 (2025/09/17) - Safety improvements: simulation by default, explicit destructive flag,
#                centralized SSH configuration, improved output formatting, comprehensive
#                input sanitization, auto-disk discovery with device type filtering  
# v1.01 (2025/09/17) - Enhanced ODF cleanup: added wipefs, targeted Ceph metadata wiping,
#                command line options, debug mode, help system
# v1.00 (orig) - Basic disk wiping with configurable block sizes

#
[ "$BASH" ] && function whence
{
	type -p "$@"
}
#
PATH_SCRIPT="$(cd $(/usr/bin/dirname $(whence -- $0 || echo $0));pwd)"
cd ${PATH_SCRIPT}

# Script version
VERSION="1.16"
SCRIPT_NAME="$(basename $0)"

# ANSI color codes (Ansible-style)
COLOR_RED='\033[0;31m'       # Errors, failures
COLOR_GREEN='\033[0;32m'     # Success, OK status
COLOR_YELLOW='\033[0;33m'    # Warnings, changes, important notices
COLOR_RESET='\033[0m'        # Reset to default

# Function to show help
show_help() {
    cat << EOF
$SCRIPT_NAME v$VERSION - Enhanced ODF/Ceph Disk Wiping Tool

USAGE:
    $SCRIPT_NAME [OPTIONS]

DESCRIPTION:
    Wipes disks on remote nodes for ODF/Ceph cleanup. Auto-discovers disks on each
    node and performs comprehensive cleanup including:
    - LUKS encrypted mapping detection and automatic closure
    - Two signature passes per disk: wipefs -fa, then sgdisk --zap-all
    - Targeted Ceph metadata wiping at strategic disk locations (0, 1GB, 10GB, 100GB, 1000GB)
    - End-of-disk metadata wiping, including the last 34 sectors (backup GPT)
    - A third signature pass only when signatures are still present
    - Clean stop of containerized ceph-osd processes that hold the disks
    - Post-wipe verification of wipefs output and of the primary and backup
      partition-table blocks, read twice after a cache flush

    Reads unique IP addresses from nodes.txt (first column) and processes all
    discoverable disks on each node, INCLUDING root disks by default.
    Only targets standard storage devices: /dev/sd*, /dev/vd*, and /dev/nvme*

    The script connects as the 'core' user and executes sudo commands on OCP nodes.

    SAFETY: By default, operations are SIMULATED only. Use explicit flag for actual wiping.

OPTIONS:
    --debug                                        Simulate disk wipe operations (default behavior)
    --yes-i-know-what-i-am-doing-please-wipe-the-disks  Perform actual disk wiping (DESTRUCTIVE!)
    --skip-rootdisk                                Skip wiping root disks on all nodes (safety option)
    -h, --help                                     Show this help message and exit
    --version                                      Show version information and exit

CONFIGURATION:
    nodes.txt format: IP_ADDRESS [ignored_disk_column]

    Configurable variables:
    - block_size: Block size in KB for operations (default: 4K)
    - metadata_count: Number of blocks for all metadata wiping operations (default: 50 blocks = 200KB)
    - skip_rootdisk: Skip wiping root disks (default: 0 - includes rootdisks)

EXAMPLES:
    $SCRIPT_NAME                                               # Simulate operations (default - safe)
    $SCRIPT_NAME --yes-i-know-what-i-am-doing-please-wipe-the-disks  # Perform actual disk wiping with LUKS cleanup
    $SCRIPT_NAME --skip-rootdisk                               # Simulate with rootdisk protection
    $SCRIPT_NAME -h                                            # Show this help

EOF
}

# Function to show version
show_version() {
    echo "$SCRIPT_NAME version $VERSION"
    echo "Enhanced ODF/Ceph disk wiping tool with auto-discovery"
}

# Parse command line arguments
debug_mode=1     # Default to simulation mode for safety
skip_rootdisk=0  # Default to include rootdisks - use --skip-rootdisk for safety
while [[ $# -gt 0 ]]; do
    case $1 in
        --debug)
            debug_mode=1
            echo -e "${COLOR_YELLOW}DEBUG MODE: Simulating disk wipe operations (no actual writes will occur)${COLOR_RESET}"
            echo "=========================================================================="
            shift
            ;;
        --yes-i-know-what-i-am-doing-please-wipe-the-disks)
            debug_mode=0
            echo -e "${COLOR_RED}LIVE MODE: Will perform ACTUAL DISK WIPING operations!${COLOR_RESET}"
            echo "=========================================================================="
            shift
            ;;
        --skip-rootdisk)
            skip_rootdisk=1
            echo -e "${COLOR_YELLOW}ROOTDISK PROTECTION: Root disks will be skipped (safe mode)${COLOR_RESET}"
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        --version)
            show_version
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information."
            exit 1
            ;;
    esac
done

# SSH configuration - connects as 'core' user to execute sudo commands on OCP nodes
# Note: Simplified to basic options for maximum compatibility
# StrictHostKeyChecking=no: Never prompt about unknown/changed host keys
# UserKnownHostsFile=/dev/null: Don't save keys (ensures no state/prompts)
# LogLevel=ERROR: Suppress "Permanently added" warnings that can contaminate output
# ConnectTimeout=5: Fail fast if host is unreachable (5 second timeout)
ssh_opts="-l core -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"

# Enhanced disk wiping for ODF/Ceph cleanup
# This script uses wipefs, targeted Ceph metadata wiping via dd at strategic offsets
block_size=4       # Block size in KB (used for all dd operations)
metadata_count=$((200 / block_size))    # Number of blocks to wipe ~200KB at each location

# Function to sanitize SSH command output (remove carriage returns, normalize whitespace)
sanitize_output() {
    echo "$1" | tr -d '\r' | xargs
}

# Validate that a string is a legitimate Linux block device name
# Only allows sd[a-z]+, vd[a-z]+, or nvme[0-9]+n[0-9]+ patterns
is_valid_disk_name() {
    local name="$1"
    [[ -n "$name" && "$name" =~ ^(nvme[0-9]+n[0-9]+|[sv]d[a-z]+)$ ]]
}

# Function to execute or simulate destructive commands
execute_or_simulate() {
    local cmd="$1"
    if [[ $debug_mode -eq 1 ]]; then
        echo "[DEBUG SIMULATE] $cmd"
        return 0
    else
        eval "$cmd"
        return $?
    fi
}

# wipefs -fa then sgdisk --zap-all, twice inside this one pass.
# The second cycle runs after partx drops the kernel partition table.
# Without that, the kernel rewrites the backup GPT (last sector) from
# memory and the next wipefs no longer sees a primary header to remove.
# Order matches the manual cleanup that still found signatures after v1.13:
# wipefs first, sgdisk --zap-all second. stderr from both is kept.
zap_signatures() {
    local ip="$1"
    local disk="$2"
    local pass="$3"

    echo "  Signature pass ${pass}: wipefs -fa then sgdisk --zap-all on /dev/${disk}"
    if [[ $debug_mode -eq 1 ]]; then
        echo "[DEBUG SIMULATE] ssh ${ssh_opts} ${ip} sudo wipefs -fa on partitions of /dev/${disk}, then on /dev/${disk}"
        echo "[DEBUG SIMULATE] ssh ${ssh_opts} ${ip} sudo sgdisk --zap-all /dev/${disk}"
        echo "[DEBUG SIMULATE] ssh ${ssh_opts} ${ip} sudo partx -d /dev/${disk}; sudo blockdev --flushbufs /dev/${disk}"
        echo "[DEBUG SIMULATE] repeat wipefs -fa and sgdisk --zap-all once the kernel table is gone"
        echo "[DEBUG SIMULATE] ssh ${ssh_opts} ${ip} sync"
        return 0
    fi

    ssh ${ssh_opts} ${ip} bash -s -- "${disk}" <<'EOS'
disk="$1"
shopt -s nullglob
if [[ "$disk" == nvme* ]]; then
    parts=(/dev/${disk}p[0-9]*)
else
    parts=(/dev/${disk}[0-9]*)
fi
for p in "${parts[@]}"; do
    echo "    wipefs -fa ${p}"
    sudo wipefs -fa "$p" || echo "    Warning: wipefs failed on ${p}"
done
cycle=1
while [[ "$cycle" -le 2 ]]; do
    echo "    cycle ${cycle}: wipefs -fa /dev/${disk}"
    sudo wipefs -fa "/dev/${disk}" || echo "    Warning: wipefs failed on /dev/${disk}"
    echo "    cycle ${cycle}: sgdisk --zap-all /dev/${disk}"
    sudo sgdisk --zap-all "/dev/${disk}" || echo "    Warning: sgdisk --zap-all returned non-zero on /dev/${disk}"
    # partx/rereadpt fail once the table is already gone. That is the clean state.
    sudo partx -d "/dev/${disk}" >/dev/null 2>&1 || true
    sudo blockdev --rereadpt "/dev/${disk}" >/dev/null 2>&1 || true
    sudo blockdev --flushbufs "/dev/${disk}" >/dev/null 2>&1 || true
    cycle=$((cycle + 1))
done
sudo udevadm settle --timeout=15 >/dev/null 2>&1 || true
sync
exit 0
EOS
}

# Anything wipefs can still name is leftover metadata. v1.13 ignored a bare
# "gpt" line, so a disk that still needed sgdisk --zap-all was reported clean.
disk_signature_report() {
    local ip="$1"
    local disk="$2"
    local remaining
    remaining=$(ssh ${ssh_opts} ${ip} "sudo wipefs -n /dev/${disk}" 2>/dev/null || true)
    remaining=$(echo "${remaining}" | tr -d '\r' | grep -viE '^DEVICE[[:space:]]+OFFSET|^$' || true)
    sanitize_output "${remaining}"
}

# RHCOS hides other users' processes from the core user. A pgrep without sudo
# reports "no OSD" while the container still has the disk open. SIGKILL while
# kubelet is up just restarts the pod, which writes the bluestore label back.
# crictl stop's default timeout is 0, which skips the graceful close.
stop_container_osds() {
    local ip="$1"
    local report line

    echo "################## OSD cleanup for node: ${ip}"
    if [[ $debug_mode -eq 1 ]]; then
        echo "  [DEBUG MODE] Would SIGTERM rook-ceph-osd containers (crictl stop --timeout 30), then SIGKILL only ceph-osd processes that are still alive"
        return 0
    fi

    report=$(ssh ${ssh_opts} ${ip} bash -s <<'EOS'
set +e
procs=$(sudo pgrep -a -x ceph-osd </dev/null 2>/dev/null || true)
pods=""
cids=""
if command -v crictl >/dev/null 2>&1; then
    pods=$(sudo crictl pods -q --name 'rook-ceph-osd' </dev/null 2>/dev/null || true)
    # The container name is "osd". "ceph-osd" matches nothing.
    cids=$(sudo crictl ps -q --name '^osd$' </dev/null 2>/dev/null || true)
fi
if [[ -z "${procs// }" && -z "${pods// }" && -z "${cids// }" ]]; then
    echo "RESULT NONE"
    exit 0
fi
echo "INFO found ceph-osd processes:"
if [[ -n "${procs// }" ]]; then
    echo "${procs}" | sed 's/^/INFO   /'
else
    echo "INFO   (none visible yet; rook-ceph-osd pods are present)"
fi
# Stop kubelet only when this node actually has OSD pods. Otherwise a
# restarted kubelet would recreate the pod between SIGTERM and the wipe.
if [[ -n "${pods// }" ]]; then
    if sudo systemctl stop kubelet </dev/null >/dev/null 2>&1; then
        echo "INFO stopped kubelet (left stopped)"
    else
        echo "INFO WARNING: kubelet did not stop"
    fi
fi
for c in ${cids}; do
    echo "INFO stopping container ${c} with SIGTERM (30s)"
    sudo crictl stop --timeout 30 "$c" </dev/null >/dev/null 2>&1 || echo "INFO WARNING: crictl stop failed for ${c}"
done
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sudo pgrep -x ceph-osd </dev/null >/dev/null 2>&1 || break
    sleep 1
done
for p in ${pods}; do
    echo "INFO stopping pod ${p}"
    sudo crictl stopp --timeout 15 "$p" </dev/null >/dev/null 2>&1 || true
done
if sudo pgrep -x ceph-osd </dev/null >/dev/null 2>&1; then
    echo "INFO sending SIGTERM to leftover ceph-osd processes"
    sudo pkill -TERM -x ceph-osd </dev/null >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sudo pgrep -x ceph-osd </dev/null >/dev/null 2>&1 || break
        sleep 1
    done
fi
if sudo pgrep -x ceph-osd </dev/null >/dev/null 2>&1; then
    echo "INFO sending SIGKILL to ceph-osd processes that did not exit"
    sudo pkill -KILL -x ceph-osd </dev/null >/dev/null 2>&1 || true
    sleep 1
fi
left=$(sudo pgrep -a -x ceph-osd </dev/null 2>/dev/null || true)
if [[ -n "${left// }" ]]; then
    echo "INFO still running:"
    echo "${left}" | sed 's/^/INFO   /'
    echo "RESULT REMAINING"
else
    echo "RESULT CLEAN"
fi
exit 0
EOS
)
    report=$(echo "$report" | tr -d '\r')
    if [[ -z "${report}" ]]; then
        echo -e "  ${COLOR_RED}WARNING: could not probe ceph-osd on ${ip}${COLOR_RESET}"
        return 0
    fi
    while IFS= read -r line; do
        case "$line" in
            INFO\ *) echo "  ${line#INFO }" ;;
            RESULT\ NONE)
                echo -e "  ${COLOR_GREEN}No ceph-osd process or rook-ceph-osd pod found${COLOR_RESET}"
                ;;
            RESULT\ CLEAN)
                echo -e "  ${COLOR_GREEN}ceph-osd has exited and released its devices${COLOR_RESET}"
                ;;
            RESULT\ REMAINING)
                echo -e "  ${COLOR_RED}WARNING: ceph-osd is still running — the wipe can be rewritten by the OSD${COLOR_RESET}"
                ;;
        esac
    done <<< "$report"
}

# Processes that have the disk or one of its partitions open.
list_disk_holders() {
    local ip="$1"
    local disk="$2"
    if [[ $debug_mode -eq 1 ]]; then
        return 0
    fi
    ssh ${ssh_opts} ${ip} bash -s -- "${disk}" <<'EOS'
disk="$1"
sudo bash -c '
disk="$1"
shopt -s nullglob
for d in /proc/[0-9]*; do
    pid=${d##*/}
    comm=$(cat "$d/comm" 2>/dev/null || true)
    for f in "$d"/fd/*; do
        t=$(readlink "$f" 2>/dev/null || true)
        hit=0
        case "$t" in
            /dev/"$disk") hit=1 ;;
            /dev/"$disk"p[0-9]|/dev/"$disk"p[0-9][0-9]) hit=1 ;;
        esac
        if [[ "$disk" != nvme* ]]; then
            case "$t" in
                /dev/"$disk"[0-9]|/dev/"$disk"[0-9][0-9]) hit=1 ;;
            esac
        fi
        if [[ "$hit" -eq 1 ]]; then
            echo "$pid $comm $t"
            break
        fi
    done
done
' bash "$disk" </dev/null
EOS
}

# Commit the page cache, flush the device, then compare a buffered read of the
# primary GPT (first 34 sectors) and the backup GPT (last 34 sectors) with a
# direct read of the same windows. Direct I/O skips the page cache, so it is
# only meaningful next to the buffered read taken after the flush. Both must
# be 17408 zero bytes and must match. wipefs cannot see a backup header once
# the primary header is gone, so the bytes are the check.
check_gpt_blocks() {
    local ip="$1"
    local disk="$2"
    ssh ${ssh_opts} ${ip} bash -s -- "${disk}" <<'EOS'
disk="$1"
fail=0
lbs=$(sudo cat /sys/block/${disk}/queue/logical_block_size 2>/dev/null || echo 512)
case "$lbs" in
    ''|*[!0-9]*) lbs=512 ;;
esac
sectors=$(sudo blockdev --getsz /dev/${disk} 2>/dev/null | tr -cd '0-9')
if [[ -z "$sectors" || "$sectors" -le 34 ]]; then
    echo "  GPT CHECK /dev/${disk}: disk size unknown, block compare skipped"
    echo "STATUS=FAIL"
    exit 0
fi

# Write dirty pages out, then ask the device to commit its own write cache.
flush_dev() {
    sync
    sudo blockdev --flushbufs /dev/${disk} >/dev/null 2>&1 || true
    if [[ "$disk" == nvme* ]] && command -v nvme >/dev/null 2>&1; then
        sudo nvme flush /dev/${disk} >/dev/null 2>&1 || sudo nvme flush /dev/${disk} -n 1 >/dev/null 2>&1 || true
    fi
    sync
}

# Write exactly 34*512 bytes starting at the given 512-byte sector into $2.
# $3 is "buffered" or "direct". Direct reads are aligned to the logical block
# size. Returns non-zero when the read itself fails.
read_window() {
    local start_sector="$1"
    local dest="$2"
    local mode="$3"
    local byte_off=$((start_sector * 512))
    local byte_len=$((34 * 512))
    local aligned_off=$((byte_off / lbs * lbs))
    local end=$((byte_off + byte_len))
    local aligned_end=$(((end + lbs - 1) / lbs * lbs))
    local aligned_len=$((aligned_end - aligned_off))
    local tmp inner flag
    # On RHCOS, sudo cannot write a file that core created in /tmp.
    # The device read has to land in a root-owned temp file.
    tmp=$(sudo mktemp /tmp/wipe-gpt.XXXXXX)
    flag=""
    if [[ "$mode" == direct ]]; then
        flag="iflag=direct"
    fi
    if ! sudo dd if=/dev/${disk} of="$tmp" bs="${lbs}" ${flag} skip=$((aligned_off / lbs)) count=$((aligned_len / lbs)) status=none </dev/null 2>/dev/null; then
        sudo rm -f "$tmp"
        return 1
    fi
    sudo chmod 644 "$tmp" 2>/dev/null || true
    inner=$((byte_off - aligned_off))
    if ! dd if="$tmp" of="$dest" bs=1 skip="${inner}" count="${byte_len}" status=none </dev/null 2>/dev/null; then
        sudo rm -f "$tmp"
        return 1
    fi
    sudo rm -f "$tmp"
    return 0
}

nonzero_bytes() {
    tr -d '\0' < "$1" | wc -c | tr -cd '0-9'
}

magic_at() {
    dd if="$1" bs=1 skip="$2" count=8 status=none </dev/null 2>/dev/null | tr -cd '[:print:]'
}

work=$(mktemp -d)
# Commit whatever the wipe left in the page cache before either read.
flush_dev
if ! read_window 0 "$work/p_buf" buffered || ! read_window $((sectors - 34)) "$work/b_buf" buffered; then
    echo "  GPT CHECK /dev/${disk}: buffered read failed"
    fail=1
fi
if ! read_window 0 "$work/p_dir" direct || ! read_window $((sectors - 34)) "$work/b_dir" direct; then
    echo "  GPT CHECK /dev/${disk}: direct read failed, buffered read is not enough"
    fail=1
fi

for pair in "primary $work/p_buf $work/p_dir 512" "backup $work/b_buf $work/b_dir $((33 * 512))"; do
    set -- $pair
    name="$1"
    buf="$2"
    dirf="$3"
    magicoff="$4"
    if [[ ! -s "$buf" || ! -s "$dirf" ]]; then
        fail=1
        continue
    fi
    b_nz=$(nonzero_bytes "$buf")
    d_nz=$(nonzero_bytes "$dirf")
    b_sz=$(wc -c < "$buf" | tr -cd '0-9')
    d_sz=$(wc -c < "$dirf" | tr -cd '0-9')
    b_magic=$(magic_at "$buf" "$magicoff")
    d_magic=$(magic_at "$dirf" "$magicoff")
    same=matched
    if ! cmp -s "$buf" "$dirf"; then
        same=DIFFERED
        fail=1
    fi
    if [[ "$b_sz" -ne 17408 || "$d_sz" -ne 17408 || "${b_nz:-1}" -ne 0 || "${d_nz:-1}" -ne 0 ]]; then
        fail=1
    fi
    echo "  GPT ${name} /dev/${disk}: buffered nonzero=${b_nz}/17408 magic='${b_magic}'"
    echo "  GPT ${name} /dev/${disk}: direct   nonzero=${d_nz}/17408 magic='${d_magic}' compare=${same}"
    if [[ "$same" == DIFFERED ]]; then
        echo "  GPT CHECK /dev/${disk}: ${name} buffered read and direct read differ after flush. Cache was not committed, or the controller cache ignored the flush."
    fi
    if [[ "${b_nz:-1}" -ne 0 || "${d_nz:-1}" -ne 0 ]]; then
        echo "  GPT CHECK /dev/${disk}: ${name} partition-table bytes are still present after flush."
        echo -n "  GPT ${name} buffered header sample:"
        od -An -tx1 -j "$magicoff" -N 16 "$buf" 2>/dev/null | sed 's/^/ /'
        echo -n "  GPT ${name} direct header sample:"
        od -An -tx1 -j "$magicoff" -N 16 "$dirf" 2>/dev/null | sed 's/^/ /'
    fi
done
rm -rf "$work"
if [[ "$fail" -eq 0 ]]; then
    echo "STATUS=OK"
else
    echo "STATUS=FAIL"
fi
exit 0
EOS
}

if [[ -x /usr/bin/dos2unix ]]; then
	/usr/bin/dos2unix nodes.txt
else
	echo "Please install dos2unix (dnf install -y dos2unix) and try again!"
	exit 1
fi

if [[ ! -f nodes.txt ]]; then
	echo "nodes.txt missing from ${PATH_SCRIPT}! exit!"
	exit 1
fi

# Extract unique IPs from nodes.txt and process each node (skip comments and empty lines)
unique_ips=$(awk '!/^#/ && NF > 0 {print $1}' nodes.txt | sort -u)

for ip in ${unique_ips}; do
    echo "################## Processing node: ${ip}"

    # Note: ssh-keygen -R was removed - it's redundant with UserKnownHostsFile=/dev/null
    # and was causing repeated "Warning: Permanently added" messages that could leak
    # into captured output and be misinterpreted as device names.

    # Test SSH connectivity before proceeding
    echo "  Testing SSH connectivity to ${ip}..."
    if ! ssh ${ssh_opts} ${ip} true 2>&1; then
        echo -e "  ${COLOR_RED}ERROR: Cannot establish SSH connection to ${ip}${COLOR_RESET}"
        echo -e "  ${COLOR_RED}ERROR: Please verify:${COLOR_RESET}"
        echo "    - Node ${ip} is reachable (ping test)"
        echo "    - SSH service is running on the node"
        echo "    - SSH key authentication is configured for 'core' user"
        echo "    - Firewall allows SSH connections"
        echo -e "  ${COLOR_YELLOW}Skipping node ${ip}${COLOR_RESET}"
        echo ""
        continue
    fi
    echo -e "  ${COLOR_GREEN}SSH connectivity OK${COLOR_RESET}"

    ssh ${ssh_opts} -qt ${ip} sudo /sbin/setenforce 0

    # Container OSDs hold the disk open. Stop them before any wipe so they
    # cannot rewrite the bluestore label or the partition table.
    stop_container_osds "${ip}"

    # Find rootdisk for this node
    # Multi-method detection with fallbacks for different RHCOS versions and states
    rootdisk=""  # Initialize as empty for each node

    # Method 1: Try /boot first (RHCOS 4.14+)
    if [[ -z "${rootdisk}" ]]; then
        boot_dev_raw=$(ssh ${ssh_opts} ${ip} sudo findmnt -no SOURCE /boot 2>/dev/null | cut -d'[' -f1)
        boot_dev=$(sanitize_output "$boot_dev_raw")
        if [[ -n "${boot_dev}" && "${boot_dev}" != /dev/loop* ]]; then
            rootdisk_raw=$(ssh ${ssh_opts} ${ip} sudo lsblk -ndo PKNAME ${boot_dev} 2>/dev/null)
            rootdisk=$(sanitize_output "$rootdisk_raw")
        fi
    fi

    # Method 2: Try /sysroot (older RHCOS)
    if [[ -z "${rootdisk}" ]]; then
        sysroot_dev_raw=$(ssh ${ssh_opts} ${ip} sudo findmnt -no SOURCE /sysroot 2>/dev/null | cut -d'[' -f1)
        sysroot_dev=$(sanitize_output "$sysroot_dev_raw")
        if [[ -n "${sysroot_dev}" && "${sysroot_dev}" != /dev/loop* ]]; then
            rootdisk_raw=$(ssh ${ssh_opts} ${ip} sudo lsblk -ndo PKNAME ${sysroot_dev} 2>/dev/null)
            rootdisk=$(sanitize_output "$rootdisk_raw")
        fi
    fi

    # Method 3: Try root (/)
    if [[ -z "${rootdisk}" ]]; then
        root_dev_raw=$(ssh ${ssh_opts} ${ip} sudo findmnt -no SOURCE / 2>/dev/null | cut -d'[' -f1)
        root_dev=$(sanitize_output "$root_dev_raw")
        if [[ -n "${root_dev}" && "${root_dev}" != /dev/loop* ]]; then
            rootdisk_raw=$(ssh ${ssh_opts} ${ip} sudo lsblk -ndo PKNAME ${root_dev} 2>/dev/null)
            rootdisk=$(sanitize_output "$rootdisk_raw")
        fi
    fi

    # Method 4: Find any disk with mounted partitions (likely root disk)
    if [[ -z "${rootdisk}" ]]; then
        rootdisk_raw=$(ssh ${ssh_opts} ${ip} \
            "sudo lsblk -npo NAME,TYPE,MOUNTPOINT 2>/dev/null | awk '\$2==\"part\" && \$3~/^\/(boot|sysroot)?\$/ {print \$1; exit}' | xargs -I {} sudo lsblk -ndo PKNAME {} 2>/dev/null | head -1" \
            2>/dev/null | sed 's@/dev/@@')
        rootdisk=$(sanitize_output "$rootdisk_raw")
    fi

    # Method 5: Fallback - find smallest non-zero disk (often root disk)
    # This is useful for ephemeral/pre-install states, exclude nbd/rbd/loop devices
    if [[ -z "${rootdisk}" ]]; then
        rootdisk_raw=$(ssh ${ssh_opts} ${ip} \
            "sudo lsblk -ndbo NAME,SIZE,TYPE 2>/dev/null | awk '\$3==\"disk\" && \$2>0 && \$1!~/^(nbd|rbd|loop)/ {print \$2\" \"\$1}' | sort -n | head -1 | awk '{print \$2}'" \
            2>/dev/null | sed 's@/dev/@@')
        rootdisk=$(sanitize_output "$rootdisk_raw")
    fi

    # Validate rootdisk name (guards against SSH warnings or garbage in parsed output)
    if [[ -n "${rootdisk}" ]] && ! is_valid_disk_name "$rootdisk"; then
        echo -e "  ${COLOR_YELLOW}WARNING: Ignoring invalid rootdisk name: '${rootdisk}'${COLOR_RESET}"
        rootdisk=""
    fi

    # Auto-discover available disks on this node (only sd*, vd*, and nvme* devices)
    echo "  Auto-discovering disks on ${ip}..."
    discovered_disks_raw=$(ssh ${ssh_opts} ${ip} \
        "sudo lsblk -dpno NAME,TYPE 2>/dev/null | \
         grep -w disk | \
         cut -f1 -d' ' | \
         cut -d/ -f3 | \
         egrep -e '^(nvme|sd|vd)'" 2>/dev/null)
    ssh_exit_code=$?
    # Sanitize and filter to only include valid disk names (remove any SSH warnings or garbage)
    # Strip carriage returns first (\r can prevent regex anchors from matching)
    discovered_disks=$(echo "$discovered_disks_raw" | tr -d '\r' | tr ' ' '\n' | grep -E '^(nvme[0-9]+n[0-9]+|[sv]d[a-z]+)$' | sort -u | tr '\n' ' ' | xargs)

    # Check if SSH command failed
    if [[ $ssh_exit_code -ne 0 ]]; then
        echo -e "  ${COLOR_RED}ERROR: Failed to execute disk discovery commands on ${ip}${COLOR_RESET}"
        echo -e "  ${COLOR_RED}ERROR: SSH command returned exit code: ${ssh_exit_code}${COLOR_RESET}"
        echo "  Output: ${discovered_disks_raw}"
        echo -e "  ${COLOR_YELLOW}Skipping node ${ip}${COLOR_RESET}"
        continue
    fi

    if [[ -z "${discovered_disks}" ]]; then
        echo -e "  ${COLOR_YELLOW}No disks discovered on ${ip}, skipping node${COLOR_RESET}"
        continue
    fi

    echo -e "  ${COLOR_GREEN}Found disks: ${discovered_disks}${COLOR_RESET}"
    echo -e "  Root disk: ${COLOR_GREEN}${rootdisk}${COLOR_RESET}"

    if [[ -z "${rootdisk}" ]]; then
        echo -e "  ${COLOR_YELLOW}WARNING: Could not detect root disk on ${ip}${COLOR_RESET}"
        if [[ ${skip_rootdisk} -eq 1 ]]; then
            echo -e "  ${COLOR_YELLOW}WARNING: --skip-rootdisk is enabled but no root disk detected${COLOR_RESET}"
            echo -e "  ${COLOR_YELLOW}WARNING: Cannot skip unknown root disk - proceeding to wipe ALL discovered disks${COLOR_RESET}"
        else
            echo -e "  ${COLOR_YELLOW}WARNING: Root disk detection failed - proceeding to wipe ALL discovered disks${COLOR_RESET}"
            echo -e "  ${COLOR_YELLOW}WARNING: If this is unintended, use --skip-rootdisk or press Ctrl-C to abort${COLOR_RESET}"
        fi
    fi

    # Step A: Handle all LUKS/crypt cleanup for this node (before disk wiping)
    echo "################## LUKS/Crypt Cleanup for node: ${ip}"
    echo "  Exploring all LUKS/crypt mappings on node ${ip}..."
    all_luks_mappings=$(ssh ${ssh_opts} ${ip} "sudo dmsetup ls --target crypt 2>/dev/null" 2>/dev/null || true)
    all_luks_mappings=$(sanitize_output "$all_luks_mappings")

    if [[ -n "${all_luks_mappings}" ]]; then
        echo "  Found crypt/LUKS devices on node:"
        echo "    ${all_luks_mappings}"

        # Filter for ODF/OCS devicesets and discovered disks
        odf_luks_mappings=$(echo "${all_luks_mappings}" | grep -E "ocs-deviceset" | cut -d$'\t' -f1 2>/dev/null || true)
        disk_luks_mappings=""
        for disk in ${discovered_disks}; do
            disk_specific=$(echo "${all_luks_mappings}" | grep -E "${disk}" | cut -d$'\t' -f1 2>/dev/null || true)
            if [[ -n "${disk_specific}" ]]; then
                disk_luks_mappings="${disk_luks_mappings} ${disk_specific}"
            fi
        done

        # Combine and deduplicate all relevant LUKS mappings
        relevant_luks_mappings=$(echo "${odf_luks_mappings} ${disk_luks_mappings}" | tr ' ' '\n' | sort -u | tr '\n' ' ')
        relevant_luks_mappings=$(sanitize_output "$relevant_luks_mappings")

        if [[ -n "${relevant_luks_mappings}" ]]; then
            echo "  Relevant LUKS mappings to close: ${relevant_luks_mappings}"
            if [[ $debug_mode -eq 0 ]]; then
                echo "  Closing LUKS mappings before disk wiping..."
                for mapping in ${relevant_luks_mappings}; do
                    echo "    Closing LUKS mapping: ${mapping}"
                    ssh ${ssh_opts} -qt ${ip} sudo cryptsetup luksClose --debug --verbose ${mapping} || echo "    Warning: Could not close ${mapping} (may already be closed)"
                done
            else
                echo "  [DEBUG MODE] Would close LUKS mappings: ${relevant_luks_mappings}"
            fi
        else
            echo "  No ODF/OCS related LUKS mappings found to close"
        fi
    else
        echo "  No crypt/LUKS devices found on this node"
    fi

    # Step B: Process each discovered disk for wiping
    echo "################## Disk Wiping for node: ${ip}"
    for disk in ${discovered_disks}; do
        # Final-gate validation: reject any non-device-name strings that slipped through
        if ! is_valid_disk_name "$disk"; then
            echo -e "  ${COLOR_YELLOW}WARNING: Skipping invalid disk name: '${disk}' (likely SSH output contamination)${COLOR_RESET}"
            continue
        fi

        # Verify the device actually exists as a block device on the remote node
        if ! ssh ${ssh_opts} ${ip} "test -b /dev/${disk}" 2>/dev/null; then
            echo -e "  ${COLOR_RED}ERROR: /dev/${disk} is not a block device on ${ip}, skipping${COLOR_RESET}"
            continue
        fi

        if [[ "${disk}" != "${rootdisk}" || "${skip_rootdisk}" -eq 0 ]]; then
            echo -e "################## Host IP: ${ip}, ${COLOR_YELLOW}Wiping disk: /dev/${disk}${COLOR_RESET} (Enhanced ODF/Ceph cleanup)"

            if [[ $debug_mode -eq 0 ]]; then
                holders=$(list_disk_holders "${ip}" "${disk}")
                holders=$(echo "$holders" | tr -d '\r' | sed '/^$/d')
                if [[ -n "${holders}" ]]; then
                    echo -e "  ${COLOR_YELLOW}WARNING: /dev/${disk} is still open:${COLOR_RESET}"
                    echo "${holders}" | sed 's/^/    /'
                    if echo "${holders}" | grep -q 'ceph-osd'; then
                        echo "  Asking the OSD to exit and release /dev/${disk}"
                        stop_container_osds "${ip}"
                        holders=$(list_disk_holders "${ip}" "${disk}")
                        holders=$(echo "$holders" | tr -d '\r' | sed '/^$/d')
                    fi
                    if [[ -n "${holders}" ]]; then
                        echo -e "  ${COLOR_RED}WARNING: /dev/${disk} is still held. The wipe may not stick:${COLOR_RESET}"
                        echo "${holders}" | sed 's/^/    /'
                    else
                        echo -e "  ${COLOR_GREEN}/dev/${disk} is no longer held${COLOR_RESET}"
                    fi
                fi
            fi

            # Step 1: First signature pass, before the offset zeroing.
            echo "Step 1: Wiping partition table and filesystem signatures (pass 1)..."
            zap_signatures "${ip}" "${disk}" 1

            # Step 2: Get disk size and prepare wipe locations
            echo "Step 2: Getting disk size and calculating wipe locations..."
            sectors_raw=$(ssh ${ssh_opts} ${ip} sudo blockdev --getsz /dev/${disk} 2>/dev/null | strings -a)
            sectors=$(sanitize_output "$sectors_raw")
            echo "  Disk /dev/${disk} has ${sectors} sectors"

            # Calculate disk size in GB (sectors * 512 bytes / 1024^3) and end-of-disk position
            if [[ -n "${sectors}" && "${sectors}" =~ ^[0-9]+$ && "${sectors}" -gt 0 ]]; then
                disk_gb=$(( sectors * 512 / 1024 / 1024 / 1024 ))
                echo "  Disk size: approximately ${disk_gb}GB"

                # Calculate end-of-disk seek position
                sectors_per_block=$(( block_size * 2 ))       # block_size KB = block_size * 2 sectors (512 bytes each)
                end_seek=$(( (sectors / sectors_per_block) - metadata_count ))

                # Filter GB offsets to only include valid ones for this disk size, then add end-of-disk
                valid_offsets=""
                for gb in 0 1 10 100 1000; do
                    if [[ $gb -lt $disk_gb ]]; then
                        valid_offsets="${valid_offsets} ${gb}GB"
                    else
                        echo "  Skipping ${gb}GB offset - beyond disk size (${disk_gb}GB)"
                    fi
                done
                # Add end-of-disk as special offset
                valid_offsets="${valid_offsets} end"
                echo "  Valid wipe locations: ${valid_offsets}"
            else
                echo "  Warning: Could not determine disk size, using standard offsets only"
                valid_offsets="0GB 1GB 10GB 100GB 1000GB"
                end_seek=0
            fi

            # Step 3: Wipe Ceph metadata locations and end of disk using ${block_size}K blocks
            echo "Step 3: Wiping Ceph metadata locations and end of disk using ${block_size}K blocks..."
            for location in ${valid_offsets}; do
                if [[ "${location}" == "end" ]]; then
                    # Handle end-of-disk wipe
                    if [[ ${end_seek} -gt 0 ]]; then
                        if [[ $debug_mode -eq 1 ]]; then
                            echo "  Wiping /dev/${disk} at end of disk (seek=${end_seek})..."
                            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${end_seek} 2>/dev/null"
                        else
                            dd_output=$(ssh ${ssh_opts} ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${end_seek} 2>&1 | grep "copied" | head -1)
                            printf "  Wiping /dev/%s at end of disk    : %s\n" "${disk}" "${dd_output}"
                        fi
                    else
                        echo "  Skipping end-of-disk wipe - could not calculate position"
                    fi
                else
                    # Handle GB offset wipes
                    gb=$(echo "${location}" | sed 's/GB$//')
                    if [[ $debug_mode -eq 1 ]]; then
                        echo "  Wiping /dev/${disk} at ${gb}GB offset..."
                        seek_blocks=$((gb * 1024 * 1024 / block_size))
                        execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${seek_blocks} 2>/dev/null"
                    else
                        seek_blocks=$((gb * 1024 * 1024 / block_size))
                        dd_output=$(ssh ${ssh_opts} ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${seek_blocks} 2>&1 | grep "copied" | head -1)
                        printf "  Wiping /dev/%s at %4sGB offset : %s\n" "${disk}" "${gb}" "${dd_output}"
                    fi
                fi
            done

            # The 4K end wipe stops on the last full 4K block. The backup GPT
            # header is the final 512-byte sector, so a size that is not a
            # multiple of 4K leaves it untouched. Buffered bs=512 works on
            # 512e and 4Kn; oflag=direct would reject 512-byte IO on 4Kn.
            echo "Step 4: Zeroing the last 34 sectors (backup GPT)..."
            if [[ -n "${sectors}" && "${sectors}" =~ ^[0-9]+$ && "${sectors}" -gt 34 ]]; then
                tail_seek=$((sectors - 34))
                execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=512 count=34 conv=fsync seek=${tail_seek}"
            else
                echo "  Skipping 512-byte tail wipe - disk size unknown or too small"
            fi
            if [[ $debug_mode -eq 0 ]]; then
                ssh ${ssh_opts} -qt ${ip} sudo blockdev --flushbufs /dev/${disk} >/dev/null 2>&1 || true
                ssh ${ssh_opts} -qt ${ip} /bin/sync
            fi

            # Step 5: Second pass. This is the wipefs + sgdisk --zap-all that
            # still had to be run by hand after v1.13. Do it after the zeros
            # are flushed, so a kernel rewrite during dd is destroyed too.
            echo "Step 5: Wiping partition table and filesystem signatures (pass 2)..."
            zap_signatures "${ip}" "${disk}" 2
        else
            echo -e "################## Host IP: ${ip}, ${COLOR_GREEN}SKIPPING rootdisk /dev/${disk}${COLOR_RESET}"
        fi
    done  # End of disk processing loop

    # Post-wipe verification: confirm no Ceph/bluestore signatures remain
    # Only run when actual wiping was performed (skip in debug/simulation mode)
    if [[ $debug_mode -eq 0 ]]; then
        echo "################## Post-wipe verification for node: ${ip}"
        verification_failed=0
        for disk in ${discovered_disks}; do
            if ! is_valid_disk_name "$disk"; then
                continue
            fi
            if [[ "${disk}" == "${rootdisk}" && "${skip_rootdisk}" -eq 1 ]]; then
                continue
            fi
            # Any remaining signature, including a bare GPT, is a failure.
            remaining_sigs=$(disk_signature_report "${ip}" "${disk}")
            if [[ -n "${remaining_sigs}" ]]; then
                echo -e "  ${COLOR_YELLOW}VERIFY: /dev/${disk} still has disk signatures, running pass 3: ${remaining_sigs}${COLOR_RESET}"
                zap_signatures "${ip}" "${disk}" 3
                remaining_sigs=$(disk_signature_report "${ip}" "${disk}")
            fi
            if [[ -n "${remaining_sigs}" ]]; then
                echo -e "  ${COLOR_RED}VERIFY FAILED: /dev/${disk} still has disk signatures after 3 passes: ${remaining_sigs}${COLOR_RESET}"
                verification_failed=1
            else
                echo -e "  ${COLOR_GREEN}VERIFY OK: /dev/${disk} has no disk signatures${COLOR_RESET}"
            fi
            gpt_report=$(check_gpt_blocks "${ip}" "${disk}")
            gpt_report=$(echo "$gpt_report" | tr -d '\r')
            echo "$gpt_report" | grep -v '^STATUS='
            if echo "$gpt_report" | grep -q '^STATUS=OK'; then
                echo -e "  ${COLOR_GREEN}VERIFY OK: /dev/${disk} primary and backup partition tables are zero in both the buffered read and the direct read${COLOR_RESET}"
            else
                echo -e "  ${COLOR_RED}VERIFY FAILED: /dev/${disk} partition-table blocks are not clean${COLOR_RESET}"
                verification_failed=1
            fi
        done
        if [[ ${verification_failed} -eq 1 ]]; then
            echo -e "  ${COLOR_RED}ERROR: Some disks on ${ip} still have signatures or partition-table bytes after wipe!${COLOR_RESET}"
            echo -e "  ${COLOR_RED}ERROR: An OSD may still be holding the disk, or the controller returned data that flush did not change.${COLOR_RESET}"
            echo -e "  ${COLOR_YELLOW}ACTION: Confirm ceph-osd is gone, then re-run this script.${COLOR_RESET}"
            exit 1
        fi
    fi
    echo -e "################## ${COLOR_GREEN}Completed processing node: ${ip}${COLOR_RESET}"
done  # End of IP processing loop
