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
| **lsnet** | LSI MegaRAID SAS controller status checker |
| **CPU_temp.sh** | CPU temperature analyzer with socket/core grouping |
| **ps_mem.py** | Show memory usage per program (not per process) |

## 🔧 System Configuration Tools

| Tool | Description |
|------|-------------|
| **RHEL_VRTS_links** | Manage Veritas Storage Foundation kernel module links on RHEL |
| **Eth2Bond** | Convert Ethernet interfaces to bonded interfaces |
| **encapsulate_rootvol.sh** | Encapsulate RHEL root filesystem into LVM2 |
| **dellfanctl** | Control Dell server fans via IPMI |

## 💾 Storage & Virtual Machine Tools

| Tool | Description |
|------|-------------|
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

## 📋 Service Files

| File | Description |
|------|-------------|
| **infoscale-modules.service** | Systemd service file for InfoScale modules |

## 🚀 Usage Examples

```bash
# Network analysis
./lseth                    # Show all network interfaces
./lseth --debug            # Show with debug information

# Memory analysis  
./lshp --size --sort       # Show hugepage usage sorted by size
./lsthp                    # Show transparent hugepage usage
./ps_mem.py                # Show memory usage per program

# File descriptor analysis
./lsfd -t 80               # Show processes using >80% of FD limit
./lskfds --size --sort     # Show killed FDs sorted by wasted space

# System monitoring
./CPU_temp.sh --by-socket  # Group temperature by CPU socket
./CPU_temp.sh --details    # Show detailed core information

# Storage management
./RHEL_VRTS_links --exec   # Execute Veritas module linking
./lsnet                    # Check RAID controller status
```

## 📝 Notes

- Tools prefixed with **ls*** follow a consistent pattern for listing system information
- Python versions (when available) offer enhanced features over shell versions
- Most tools include `--debug` and `--help` options for detailed information
- Many tools require root privileges for full functionality

## 🏗️ Architecture

This toolset includes both original shell scripts and enhanced Python rewrites:
- **Enhanced Python versions**: Improved error handling, debug output, and additional features
- **Original shell versions**: Preserved for compatibility and reference
- **Consistent interface**: Similar command-line options across related tools