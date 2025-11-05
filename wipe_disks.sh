#!/bin/bash

# Enhanced ODF/Ceph Disk Wiping Tool
# 
# VERSION HISTORY:
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
VERSION="1.10"
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
ssh_opts="-l core -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

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

# Extract unique IPs from nodes.txt and process each node (skip comments and empty lines)
unique_ips=$(awk '!/^#/ && NF > 0 {print $1}' nodes.txt | sort -u)

for ip in ${unique_ips}; do
    echo "################## Processing node: ${ip}"
    ssh-keygen -R "${ip}" > /dev/null 2>&1

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

    # Auto-discover available disks on this node (only sd*, vd*, and nvme* devices)
    echo "  Auto-discovering disks on ${ip}..."
    discovered_disks_raw=$(ssh ${ssh_opts} ${ip} \
        "sudo lsblk -dpno NAME,TYPE 2>/dev/null | \
         grep -w disk | \
         cut -f1 -d' ' | \
         cut -d/ -f3 | \
         egrep -e '^(nvme|sd|vd)'" 2>&1)
    ssh_exit_code=$?
    discovered_disks=$(sanitize_output "$discovered_disks_raw")

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
        if [[ "${disk}" != "${rootdisk}" || "${skip_rootdisk}" -eq 0 ]]; then
            echo -e "################## Host IP: ${ip}, ${COLOR_YELLOW}Wiping disk: /dev/${disk}${COLOR_RESET} (Enhanced ODF/Ceph cleanup)"

            # Step 1: Wipe the partition table off the disk to a fresh, usable state
            echo "Step 1: Wiping partition table and filesystem signatures..."
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo sgdisk -Z /dev/${disk} 2>/dev/null"
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo wipefs -fa /dev/${disk}"

            # Step 2: Get disk size and prepare wipe locations
            echo "Step 2: Getting disk size and calculating wipe locations..."
            sectors_raw=$(ssh ${ssh_opts} ${ip} sudo blockdev --getsz /dev/${disk} 2>/dev/null | strings -a)
            sectors=$(sanitize_output "$sectors_raw")
            echo "  Disk /dev/${disk} has ${sectors} sectors"

            # Calculate disk size in GB (sectors * 512 bytes / 1024^3) and end-of-disk position
            if [[ -n "${sectors}" && "${sectors}" -gt 0 ]]; then
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

            # Step 4: Attempt block discard (if supported by device)
            echo "Step 4: Attempting block discard (if supported by device)..."
            execute_or_simulate "ssh ${ssh_opts} -qt ${ip} sudo blkdiscard /dev/${disk} 2>/dev/null" && echo -e "  ${COLOR_GREEN}Block discard successful${COLOR_RESET}" || echo -e "  ${COLOR_YELLOW}Block discard not supported or failed (this is normal for some devices)${COLOR_RESET}"

            # Step 5: Final sync
            ssh ${ssh_opts} -qt ${ip} /bin/sync
        else
            echo -e "################## Host IP: ${ip}, ${COLOR_GREEN}SKIPPING rootdisk /dev/${disk}${COLOR_RESET}"
        fi
    done  # End of disk processing loop
    echo -e "################## ${COLOR_GREEN}Completed processing node: ${ip}${COLOR_RESET}"
done  # End of IP processing loop
