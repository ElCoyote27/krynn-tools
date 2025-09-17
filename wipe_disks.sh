#!/bin/bash

# Enhanced ODF/Ceph Disk Wiping Tool
# 
# VERSION HISTORY:
# v1.02 (2024) - Safety improvements: simulation by default, explicit destructive flag,
#                centralized SSH configuration, improved output formatting, comprehensive
#                input sanitization, auto-disk discovery with device type filtering
# v1.01 (2024) - Enhanced ODF cleanup: added wipefs, targeted Ceph metadata wiping,
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
VERSION="1.02"
SCRIPT_NAME="$(basename $0)"

# Function to show help
show_help() {
    cat << EOF
$SCRIPT_NAME v$VERSION - Enhanced ODF/Ceph Disk Wiping Tool

USAGE:
    $SCRIPT_NAME [OPTIONS]

DESCRIPTION:
    Wipes disks on remote nodes for ODF/Ceph cleanup. Auto-discovers disks on each
    node and performs comprehensive cleanup including:
    - Partition table and filesystem signature removal (wipefs)
    - Targeted Ceph metadata wiping at strategic disk locations (0, 1GB, 10GB, 100GB, 1000GB)
    - End-of-disk metadata wiping (~200KB at end of disk)
    - Block discard operations (when supported)

    Reads unique IP addresses from nodes.txt (first column) and processes all
    discoverable disks on each node, INCLUDING root disks by default.
    Only targets standard storage devices: /dev/sd*, /dev/vd*, and /dev/nvme*
    
    The script connects as the 'core' user and executes sudo commands on OCP nodes.
    
    SAFETY: By default, operations are SIMULATED only. Use explicit flag for actual wiping.

OPTIONS:
    --debug                                        Simulate disk wipe operations (default behavior)
    --yes-i-know-what-i-am-doing-please-wipe-the-disks  Perform actual disk wiping (DESTRUCTIVE!)
    --skip-rootdisk                                Skip wiping root disks on all nodes (safety option)
    --help                                         Show this help message and exit
    --version                                      Show version information and exit

CONFIGURATION:
    nodes.txt format: IP_ADDRESS [ignored_disk_column]
    
    Configurable variables:
    - block_size: Block size in KB for operations (default: 4K)
    - metadata_count: Number of blocks for all metadata wiping operations (default: 50 blocks = 200KB)
    - skip_rootdisk: Skip wiping root disks (default: 0 - includes rootdisks)

EXAMPLES:
    $SCRIPT_NAME                                               # Simulate operations (default - safe)
    $SCRIPT_NAME --yes-i-know-what-i-am-doing-please-wipe-the-disks  # Perform actual disk wiping
    $SCRIPT_NAME --skip-rootdisk                               # Simulate with rootdisk protection
    $SCRIPT_NAME --help                                        # Show this help

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
            echo "DEBUG MODE: Simulating disk wipe operations (no actual writes will occur)"
            echo "=========================================================================="
            shift
            ;;
        --yes-i-know-what-i-am-doing-please-wipe-the-disks)
            debug_mode=0
            echo "LIVE MODE: Will perform ACTUAL DISK WIPING operations!"
            echo "=========================================================================="
            shift
            ;;
        --skip-rootdisk)
            skip_rootdisk=1
            echo "ROOTDISK PROTECTION: Root disks will be skipped (safe mode)"
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        --version)
            show_version
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# SSH configuration - connects as 'core' user to execute sudo commands on OCP nodes
ssh_opts="-l core -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

# Enhanced disk wiping for ODF/Ceph cleanup
# This script now uses wipefs, targeted Ceph metadata wiping, and blkdiscard
block_size=4       # Block size in KB (used for all dd operations)
metadata_count=$((200 / block_size))    # Number of blocks to wipe ~200KB at each location

# Function to sanitize SSH command output (remove carriage returns, normalize whitespace)
sanitize_output() {
    echo "$1" | tr -d '\r' | xargs
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

# Extract unique IPs from nodes.txt and process each node
unique_ips=$(awk '{print $1}' nodes.txt | sort -u)

for ip in ${unique_ips}; do
    echo "################## Processing node: ${ip}"
    ssh-keygen -R "${ip}" > /dev/null 2>&1

    ssh ${ssh_opts} -qt ${ip} sudo /sbin/setenforce 0

    # Find rootdisk for this node
    rootdisk_raw=$(ssh ${ssh_opts} -qt ${ip} sudo /usr/bin/findmnt -nv -o SOURCE / 2>/dev/null | \
    strings -a|sed -e 's@[0-9]$@@' -e 's@/dev/@@')
    rootdisk=$(sanitize_output "$rootdisk_raw")
    
    # Auto-discover available disks on this node (only sd*, vd*, and nvme* devices)
    echo "  Auto-discovering disks on ${ip}..."
    discovered_disks_raw=$(ssh ${ssh_opts} -qt ${ip} \
        "sudo lsblk -dpno NAME,TYPE 2>/dev/null | \
         grep -w disk | \
         cut -f1 -d' ' | \
         cut -d/ -f3 | \
         egrep -e '^(nvme|sd|vd)'" 2>/dev/null)
    discovered_disks=$(sanitize_output "$discovered_disks_raw")
    
    if [[ -z "${discovered_disks}" ]]; then
        echo "  No disks discovered on ${ip}, skipping node"
        continue
    fi
    
    echo "  Found disks: ${discovered_disks}"
    echo "  Root disk: ${rootdisk}"
    
    # Process each discovered disk
    for disk in ${discovered_disks}; do
        echo "################## Host IP: ${ip}, Processing disk: /dev/${disk}"
        
        # Disk already verified by lsblk auto-discovery - no need for additional accessibility test

        if [[ "${disk}" != "${rootdisk}" || "${skip_rootdisk}" -eq 0 ]]; then
            echo "################## Host IP: ${ip}, Wiping disk: /dev/${disk} (Enhanced ODF/Ceph cleanup)"

            # Step 1: Wipe the partition table off the disk to a fresh, usable state
            echo "Step 1: Wiping partition table and filesystem signatures..."
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo sgdisk -Z /dev/${disk} 2>/dev/null"
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo wipefs -fa /dev/${disk}"

            # Step 2: Wipe certain areas of the disk to remove Ceph Metadata which may be present
            echo "Step 2: Wiping Ceph metadata locations (0, 1GB, 10GB, 100GB, 1000GB offsets) using ${block_size}K blocks..."
            # Using precalculated metadata_count (~200KB) at each strategic location
            for gb in 0 1 10 100 1000; do
                if [[ $debug_mode -eq 1 ]]; then
                    echo "  Wiping /dev/${disk} at ${gb}GB offset..."
                    seek_blocks=$((gb * 1024 * 1024 / block_size))
                    execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${seek_blocks} 2>/dev/null"
                else
                    seek_blocks=$((gb * 1024 * 1024 / block_size))
                    dd_output=$(ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} oflag=direct,dsync seek=${seek_blocks} 2>&1 | grep "copied" | head -1)
                    printf "  Wiping /dev/%s at %4sGB offset : %s\n" "${disk}" "${gb}" "${dd_output}"
                fi
            done

            # Step 3: Wipe end of disk (same amount as metadata locations)
            echo "Step 3: Wiping end of disk (~200KB using ${block_size}K blocks)..."
            sectors_raw=$(ssh ${ssh_opts} -qt ${ip} sudo blockdev --getsz /dev/${disk} 2>/dev/null | strings -a)
            sectors=$(sanitize_output "$sectors_raw")
            echo "  Disk /dev/${disk} has ${sectors} sectors"
            # Calculate seek position using metadata_count
            sectors_per_block=$(( block_size * 2 ))       # block_size KB = block_size * 2 sectors (512 bytes each)
            if [[ -n "${sectors}" && "${sectors}" -gt 0 ]]; then
                seek=$(( (sectors / sectors_per_block) - metadata_count ))
                echo "  Calculated seek position: ${seek} (sectors_per_block=${sectors_per_block}, metadata_count=${metadata_count})"
            else
                seek=-1
                echo "  Could not determine disk size, skipping end-of-disk wipe"
            fi
            if [[ $seek -gt 0 ]]; then
                if [[ $debug_mode -eq 1 ]]; then
                    execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} seek=${seek} oflag=direct,dsync"
                    echo "  End-of-disk wipe completed"
                else
                    dd_output=$(ssh ${ssh_opts} -qt ${ip} sudo dd if=/dev/zero of=/dev/${disk} bs=${block_size}K count=${metadata_count} seek=${seek} oflag=direct,dsync 2>&1 | grep "copied" | head -1)
                    printf "  Wiping /dev/%s at end-of-disk     : %s\n" "${disk}" "${dd_output}"
                fi
            else
                echo "  Disk too small for end-of-disk wipe, skipping"
            fi

            # Step 4: Attempt block discard (might not be supported on all devices)
            echo "Step 4: Attempting block discard (if supported by device)..."
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo blkdiscard /dev/${disk} 2>/dev/null" && echo "  Block discard successful" || echo "  Block discard not supported or failed (this is normal for some devices)"

            # Step 5: Final sync
            ssh ${ssh_opts} -qt ${ip} /bin/sync
        else
            echo "################## Host IP: ${ip}, SKIPPING rootdisk /dev/${disk}"
        fi
    done  # End of disk processing loop
    echo "################## Completed processing node: ${ip}"
done  # End of IP processing loop
