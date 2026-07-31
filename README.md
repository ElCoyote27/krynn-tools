# krynn-tools

A collection of system administration and diagnostic tools for Linux environments.

## 📊 System Analysis & Monitoring Tools

| Tool | Description |
|------|-------------|
| **lseth** | List network interface details (speed, driver, MAC, IP, PCI path) |
| **lsfd** | List file descriptor usage per process and user |
| **lshp** | List hugepages in use, showing processes and KVM guest names |
| **lsthp** | List transparent hugepages usage by process |
| **lskfds** | List killed (deleted) file descriptors preventing disk space recovery |
| **lsNVMe.py** | NVMe device health and temperature monitor with SMART attributes |
| **lsPCISpeeds.py** | PCI device speed analyzer showing max/negotiated speeds and lane config |
| **megaclisas-status** | LSI MegaRAID SAS controller status checker |
| **CPU_temp.py** | CPU temperature analyzer with socket/core grouping (Python rewrite of CPU_temp.sh) |
| **ps_mem.py** | Show memory usage per program (not per process) |
| **meminfo-gap.sh** | Show unaccounted kernel memory from /proc/meminfo |

## 🔧 System Configuration Tools

| Tool | Description |
|------|-------------|
| **sysctl_manager.py** | Manage and compare sysctl kernel tunables against desired profiles |
| **TunedReconfig.py** | Switch between tuned profiles (powersave, virtual-host, etc.) |
| **ext4_feature_upgrade.sh** | Audit and upgrade ext4 filesystem features (64bit, metadata_csum) |
| **RHEL_VRTS_links** | Manage Veritas Storage Foundation kernel module links on RHEL |
| **Eth2Bond** | Convert Ethernet interfaces to bonded interfaces |
| **encapsulate_rootvol.sh** | Encapsulate RHEL root filesystem into LVM2 |
| **dellfanctl** | Control Dell server fans via IPMI |
| **apc_ups_outlet_reboot.py** | Reboot APC SmartUPS outlet groups via SSH |

## ☸ OpenShift / Kubernetes Tools

| Tool | Description |
|------|-------------|
| **odf-storage-report.py** | ODF storage consumption report (Ceph, PVCs, Quay, ACM) |
| **odf-storage-report.sh** | ODF storage consumption report (legacy shell version) |
| **wipe_disks.sh** | ODF/Ceph disk wiping tool with LUKS cleanup and multi-method root disk detection |
| **bfg-acm-thanos-s3.sh** | Fix ACM Observability by purging and recreating a full Thanos S3 bucket |
| **ocp_disconnected_optouts.sh** | Disable Insights, telemetry, and CDI imports for disconnected OCP clusters |

## 💾 Storage & Virtual Machine Tools

| Tool | Description |
|------|-------------|
| **rsync_KVM_OS.py** | KVM virtual machine replication between hypervisors with VXFS snapshot support |
| **rsync_KVM_OS.sh** | KVM virtual machine replication (legacy shell version) |
| **Qemu_Find_Next_MACs.py** | Find next free MAC addresses for KVM guests avoiding collisions |
| **vxfs_recycle_snapshot.py** | Test VXFS snapshot create/mount/cleanup cycle |
| **convert_qcow2_to_compressed_v3.sh** | Convert QCOW2 images to compressed v3 format |
| **convert_qcow2_to_uncompressed_v3.sh** | Convert QCOW2 images to uncompressed v3 format |
| **convert_qcow2_to_uncompressed_v3_non_sparse.sh** | Convert QCOW2 images to non-sparse uncompressed v3 |
| **samsung_ssd_decode.py** | Decode Samsung SSD firmware .enc files |

## 🛠️ System Utilities

| Tool | Description |
|------|-------------|
| **ptree** | Display process tree for a given PID |
| **FixNames.pl** | Perl script to fix file and directory names |
| **FixPerms.sh** | Fix file and directory permissions |
| **GoUpper.sh** | Convert filenames to uppercase |
| **ShFmt.pl** | Perl script for shell script formatting |
| **eXpSpaces.sh** | Strip trailing whitespace from files |

## 📋 Service Files

| File | Description |
|------|-------------|
| **infoscale-modules.service** | Systemd service file for InfoScale modules |

## 🧪 Test & Support Scripts

| File | Description |
|------|-------------|
| **non_reg_megaclisas.sh** | Regression test for megaclisas-status across hosts |
| **test_xml_normalize.py** | Unit tests for XML normalization in rsync_KVM_OS.py |

## 🚀 Usage Examples

```bash
# Network analysis
./lseth                    # Show all network interfaces
./lseth --debug            # Show with debug information

# Hardware analysis
./lsNVMe.py                # Show NVMe device health and temperatures
./lsNVMe.py --noserials    # Hide serial numbers
./lsPCISpeeds.py            # Show PCI device speeds and lane config

# Memory analysis
./lshp --size --sort       # Show hugepage usage sorted by size
./lsthp                    # Show transparent hugepage usage
./ps_mem.py                # Show memory usage per program
./meminfo-gap.sh           # Show unaccounted kernel memory

# File descriptor analysis
./lsfd -t 80               # Show processes using >80% of FD limit
./lskfds --size --sort     # Show killed FDs sorted by wasted space

# System monitoring
./CPU_temp.py --by-socket  # Group temperature by CPU socket
./CPU_temp.py --details    # Show detailed core information

# System configuration
./sysctl_manager.py compare @hvm    # Compare sysctls against HVM profile
./sysctl_manager.py apply @server   # Apply server sysctl profile
./TunedReconfig.py                  # Switch tuned profile interactively
./ext4_feature_upgrade.sh           # Audit ext4 features (dry-run)
./ext4_feature_upgrade.sh --upgrade # Upgrade ext4 features

# OpenShift / ODF
export KUBECONFIG=/path/to/kubeconfig
./odf-storage-report.py            # ODF storage consumption report
./wipe_disks.sh --help              # ODF/Ceph disk wiping
./bfg-acm-thanos-s3.sh             # Purge and recreate Thanos S3 bucket

# Storage management
./rsync_KVM_OS.py --help            # KVM VM replication
./Qemu_Find_Next_MACs.py            # Find next free KVM MAC addresses
./RHEL_VRTS_links --exec            # Execute Veritas module linking
./megaclisas-status                  # Check RAID controller status
```

## 📝 Notes

- Tools prefixed with **ls*** follow a consistent pattern for listing system information
- Python versions (when available) offer enhanced features over shell versions
- Most tools include `--debug` and `--help` options for detailed information
- Many tools require root privileges for full functionality
- OpenShift tools require `oc` in PATH and a valid `KUBECONFIG`

## 🏗️ Architecture

This toolset includes both original shell scripts and enhanced Python rewrites:
- **Enhanced Python versions**: Improved error handling, debug output, and additional features
- **Original shell versions**: Preserved for compatibility and reference
- **Consistent interface**: Similar command-line options across related tools
