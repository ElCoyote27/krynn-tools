#!/usr/bin/env python3
"""
KVM Virtual Machine Replication Script

This script replicates KVM virtual machines from one hypervisor to another.
It supports VXFS snapshots for consistent backups and handles various
hypervisor-specific configurations.

Author: Converted from bash script
"""

# $Id: rsync_KVM_OS.py,v 1.02 2025/09/02 18:30:00 python-conversion Exp $
__version__ = "rsync_KVM_OS.py,v 1.02 2025/09/02 18:30:00 python-conversion Exp"

#
# VERSION HISTORY:
# ================
#
# v1.02 (2025-09-02): CLI host override and validation enhancements
#   - Added --host and --dest-host arguments to override destination host from CLI
#   - Enhanced flexibility: CLI override takes precedence over script name auto-detection
#   - SSH connectivity check always performed regardless of host determination method
#   - Added validate_remote_host() method for common hostname validation logic
#   - Improved logging to show host determination method (CLI vs auto-detected)
#   - Maintains backward compatibility: existing script-name-based workflow unchanged
#
# v1.01 (2025-09-02): Critical bugfixes and stat binary enhancements
#   - BUGFIX: Fixed stat command testing to use system stat locally, custom path only for remote
#   - Enhanced stat availability checking on both source AND destination systems
#   - Added intelligent fallback to rsync-based comparison when stat unavailable
#   - Fixed tool copying to sync entire script directory (not just single file)
#   - BUGFIX: Removed trailing slash in rsync directory copy to preserve directory structure
#   - Added comprehensive dual-system stat testing with proper error handling
#   - Improved NAS host support with configurable stat_path (/opt/bin/stat)
#   - Enhanced debug output showing correct executed script paths
#   - Performance optimization: only syncs files that actually changed via stat comparison
#
# v1.00 (2025-09-02): Initial Python conversion from bash script
#   - Complete Python rewrite of rsync_KVM_OS.sh with feature parity
#   - Consolidated host-specific configurations into single configuration table
#   - Enhanced debug mode: performs all checks and logic, uses rsync --dry-run
#   - Smart defaults with KVM_STD_CONFIG and NAS_STD_CONFIG templates
#   - Improved error handling and logging throughout
#   - VXFS snapshot support with graceful handling of existing snapshots
#   - Parallel rsync operations using xargs -P for maximum bandwidth
#   - Root privilege enforcement and proper configuration centralization
#   - Supports --debug for dry-run testing and --force for bypassing checks
#

import argparse
import os
import sys
import subprocess
import xml.etree.ElementTree as ET
import time
import socket
import stat
import shutil
import signal
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import logging
import psutil

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='(%(levelname)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION SECTION - Modify these values as needed
# =============================================================================

# Default Paths (Source directories and templates)
DEFAULT_KVM_CONF_SRC_DIR = "/etc/libvirt/qemu"
DEFAULT_KVM_CONF_DST_DIR = "/etc/libvirt/qemu"
DEFAULT_KVM_IMAGES_SRC_DIRS = ["/shared/kvm0/images"]
DEFAULT_KVM_NVRAM_SRC_DIRS = ["/shared/kvm0/nvram"]
DEFAULT_KVM_TEMPLATES = "/var/lib/libvirt/templates"

# SSH and Rsync Configuration
SSH_CIPHER = "aes128-gcm@openssh.com"
RSYNC_OPTIONS = "-a --info=name,progress1 --delete --whole-file --skip-compress=qcow2"

# VXFS Snapshot Configuration
VXSNAP_PREFIX = "/run/user/0"  # Always root user
VXSNAP_OPTIONS = "cachesize=1536g/autogrow=yes"
VXFS_SNAPSHOTS_ENABLED = True

# Timing Configuration
WAIT_TIME_BEFORE_SYNC = 2.5  # seconds

# Default VM List - VMs to replicate by default
DEFAULT_VM_LIST = [
    "dc00", "dc01", "dc02", "dc03", "fedora-x64", "fedora-csb-x64", 
    "win10-x64", "win11-x64", "unifi", "gitlab",
    "bdc416x", "bdc417x", "bdc418x", "bdc419x", "bdc420x", "bdc421x", 
    "bdc422x", "bdc423x",
    "sat6", "ca8", "idm00", "registry", "quay", "vxvom", "www8", 
    "kali-x64", "freenas-11", "ubuntu-x64", "dsm7", "sno4",
    "rhel3-x86", "rhel4-x86", "rhel5-x86", "rhel5-x64", "rhel6-x86", 
    "rhel6-x64", "rhel7-x64", "rhel8-x64", "rhel8-x64-eus", "rhel9-x64",
    "coreos-sno-0", "coreos-sno-1", "coreos-sno-2", "coreos-sno-3", "cirros"
]

# =============================================================================
# END CONFIGURATION SECTION
# =============================================================================


@dataclass
class HostConfig:
    """Configuration for a specific remote host."""
    remote_host: str = ""  # Will default to detected hostname if empty
    threads: int = 1
    kvm_images_dst_dirs: List[str] = None
    kvm_nvram_dst_dirs: List[str] = None
    default_vm_list: str = ""
    rsync_path: str = ""
    stat_path: str = ""  # Path to stat binary (e.g., /opt/bin/stat for NAS)
    skip_define: bool = False
    vxfs_snapshots: bool = VXFS_SNAPSHOTS_ENABLED
    skip_mount_check: bool = False  # Skip remote mount point verification
    skip_stat_check: bool = False   # Skip file stat comparison checks

    def __post_init__(self):
        # Use standard KVM configuration as defaults
        if self.kvm_images_dst_dirs is None:
            self.kvm_images_dst_dirs = ["/shared/kvm0/images"]
        if self.kvm_nvram_dst_dirs is None:
            self.kvm_nvram_dst_dirs = ["/shared/kvm0/nvram"]

    def get_effective_remote_host(self, detected_hostname: str) -> str:
        """Get the effective remote host, using detected hostname if not specified."""
        return self.remote_host if self.remote_host else detected_hostname


class KVMReplicator:
    """Main class for KVM VM replication operations."""

    def __init__(self):
        # Runtime configuration - set by command line args
        self.force_checksum = False
        self.force_action = False
        self.poweroff = False
        self.test_only = False
        self.update_only = False
        self.debug = False

        # Use configuration constants
        self.wait_time = WAIT_TIME_BEFORE_SYNC
        self.vxfs_snapshots = VXFS_SNAPSHOTS_ENABLED
        self.vxsnap_prefix = VXSNAP_PREFIX
        self.vxsnap_opts = VXSNAP_OPTIONS

        # Default paths from configuration
        self.kvm_conf_src_dir = DEFAULT_KVM_CONF_SRC_DIR
        self.kvm_conf_dst_dir = DEFAULT_KVM_CONF_DST_DIR
        self.kvm_images_src_dirs = DEFAULT_KVM_IMAGES_SRC_DIRS.copy()
        self.kvm_nvram_src_dirs = DEFAULT_KVM_NVRAM_SRC_DIRS.copy()
        self.default_kvm_templates = DEFAULT_KVM_TEMPLATES

        # SSH and rsync configuration from constants
        self.ssh_cipher = SSH_CIPHER
        self.rsync_options = RSYNC_OPTIONS

        # Default VM list from configuration
        self.default_vm_list = DEFAULT_VM_LIST.copy()

        # Host-specific configurations
        self.host_configs = self._init_host_configs()

        # Current configuration
        self.remote_host = ""
        self.host_config = None
        self.stat_available = True  # Track if stat works on both source and destination

        # Process tracking for proper cleanup
        self.child_processes = []  # Track child processes for cleanup

    def _init_host_configs(self) -> Dict[str, HostConfig]:
        """Initialize host-specific configurations."""
        configs = {}
        default_vm_list = " ".join(DEFAULT_VM_LIST)

        # =============================================================================
        # HOST CONFIGURATION TABLE  
        # =============================================================================
        # Only specify values that differ from defaults:
        # - remote_host: "" (defaults to detected hostname)
        # - threads: 1
        # - rsync_path: "" (uses system default rsync)
        # - stat_path: "" (uses system default stat)
        # - vxfs_snapshots: VXFS_SNAPSHOTS_ENABLED
        # - default_vm_list: default_vm_list
        # - skip_mount_check: False
        # - skip_stat_check: False
        # - Standard KVM paths: ["/shared/kvm0/images"], ["/shared/kvm0/nvram"]

        # Standard configuration templates
        KVM_STD_CONFIG = {
            'kvm_images_dst_dirs': ["/shared/kvm0/images"],
            'kvm_nvram_dst_dirs': ["/shared/kvm0/nvram"],
            'rsync_path': "",  # Use default rsync
            'stat_path': "",   # Use default stat (system PATH)
            'skip_define': False,
            'skip_mount_check': False,
            'skip_stat_check': False
        }

        NAS_STD_CONFIG = {
            'kvm_images_dst_dirs': ["/volume1/kvm0/images"],
            'kvm_nvram_dst_dirs': ["/volume1/kvm0/nvram"], 
            'rsync_path': "/opt/bin/rsync",
            'stat_path': "/opt/bin/stat",  # NAS-specific stat binary
            'skip_define': True,
            'skip_mount_check': True,
            'skip_stat_check': False  # Now we can do stat checks with correct path!
        }

        host_configs_table = {
            # -228 management interface hosts (use KVM standard + remote host override)
            'daltigoth': {**KVM_STD_CONFIG, 'remote_host': 'daltigoth-228', 'threads': 2},
            'daltigoth-228': {**KVM_STD_CONFIG, 'remote_host': 'daltigoth-228', 'threads': 2},
            'palanthas': {**KVM_STD_CONFIG, 'remote_host': 'palanthas-228', 'threads': 2},
            'palanthas-228': {**KVM_STD_CONFIG, 'remote_host': 'palanthas-228', 'threads': 2},
            'ravenvale': {**KVM_STD_CONFIG, 'remote_host': 'ravenvale-228', 'vxfs_snapshots': False},
            'ravenvale-228': {**KVM_STD_CONFIG, 'remote_host': 'ravenvale-228', 'vxfs_snapshots': False},

            # Standard KVM hosts (only override VM lists)
            'solinari': {**KVM_STD_CONFIG,
                'default_vm_list': "rhel3-x86 win10-x64 win11-x64 bdc420x dc00 dc01 idm00 fedora-x64 fedora-csb-x64 cirros ca8"
            },
            'solanthus': {**KVM_STD_CONFIG,
                'default_vm_list': "rhel3-x86 rhel9-x64 ca8 fedora-x64 fedora-csb-x64 win10-x64 win11-x64 dc00 dc01 bdc420x idm00 cirros"
            },
            'lothlorien': {**KVM_STD_CONFIG,
                'default_vm_list': "fedora-x64 cirros"
            },

            # NAS/Synology hosts (use NAS standard + thread overrides)
            'kalaman': {**NAS_STD_CONFIG, 'threads': 2},
            'ligett': {**NAS_STD_CONFIG},

            # Testing hosts (use KVM standard + limited VM lists)
            'rh8x64': {**KVM_STD_CONFIG, 'default_vm_list': "win11-x64 cirros"},
            'rh9x64': {**KVM_STD_CONFIG, 'default_vm_list': "win11-x64 cirros"}
        }

        # =============================================================================
        # BUILD CONFIGURATIONS FROM TABLE
        # =============================================================================

        for hostname, config_values in host_configs_table.items():
            # Apply default VM list if not already specified in the config
            if 'default_vm_list' not in config_values:
                config_values = {**config_values, 'default_vm_list': default_vm_list}

            configs[hostname] = HostConfig(**config_values)

        return configs

    def validate_remote_host(self, hostname: str) -> str:
        """Validate a remote hostname (from CLI or script name detection)."""
        if not hostname:
            logger.error("Empty hostname provided!")
            sys.exit(127)

        # Validate hostname resolution
        try:
            socket.gethostbyname(hostname)
        except socket.gaierror:
            logger.error(f"Unable to resolve host \"{hostname}\"")
            sys.exit(127)

        # Check we're not running on the target host
        if hostname == socket.gethostname():
            logger.error(f"Don't run this on {hostname} to push files to {hostname}!")
            sys.exit(127)

        return hostname

    def cleanup_child_processes(self):
        """Clean up only child processes spawned by this script."""
        if not self.child_processes:
            return

        logger.info("Cleaning up child processes...")
        for process in self.child_processes[:]:  # Create a copy to iterate over
            try:
                if process.poll() is None:  # Process is still running
                    logger.debug(f"Terminating child process {process.pid}")

                    # Try to get the process and its children
                    try:
                        parent = psutil.Process(process.pid)
                        children = parent.children(recursive=True)

                        # Terminate children first
                        for child in children:
                            try:
                                child.terminate()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass

                        # Terminate the parent
                        parent.terminate()

                        # Wait for graceful termination
                        try:
                            parent.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            # Force kill if necessary
                            try:
                                parent.kill()
                                for child in children:
                                    try:
                                        child.kill()
                                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                                        pass
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass

                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        # Process already gone or no access
                        pass

                    # Remove from our tracking list
                    self.child_processes.remove(process)

            except Exception as e:
                logger.debug(f"Error cleaning up process: {e}")
                # Remove it anyway to avoid keeping dead references
                if process in self.child_processes:
                    self.child_processes.remove(process)

    def get_remote_host_from_script_name(self) -> str:
        """Extract remote host name from script basename."""
        script_name = os.path.basename(sys.argv[0])
        # Remove rsync_KVM_ prefix and _OS.py suffix
        remote_host = script_name.replace('rsync_KVM_', '').replace('_OS.py', '').replace('.py', '')

        if not remote_host:
            logger.error("Unable to guess Remote host from script name!")
            sys.exit(127)

        # Use common validation logic
        return self.validate_remote_host(remote_host)

    def setup_host_config(self, detected_hostname: str):
        """Set up configuration for the specified remote host."""        
        if detected_hostname in self.host_configs:
            self.host_config = self.host_configs[detected_hostname]
        else:
            # Use default config (remote_host will default to detected_hostname)
            self.host_config = HostConfig(default_vm_list=" ".join(DEFAULT_VM_LIST))

        # Determine effective remote host
        self.remote_host = self.host_config.get_effective_remote_host(detected_hostname)

        logger.info(f"Remote destination: {self.remote_host}")

    def test_stat_availability(self):
        """Test if stat command works on both local and remote systems."""
        logger.info("Testing stat command availability...")

        # Test local stat (always use system default "stat" locally)
        try:
            result = subprocess.run(["stat", "--version"], 
                                  capture_output=True, text=True, check=False)
            if result.returncode != 0:
                logger.warning("Local stat command not working properly")
                self.stat_available = False
                return
        except Exception as e:
            logger.warning(f"Local stat command test failed: {e}")
            self.stat_available = False
            return

        # Test remote stat
        try:
            remote_stat_cmd = self.host_config.stat_path if self.host_config.stat_path else "stat"
            result = self.run_ssh_command(f"{remote_stat_cmd} --version", check=False)
            if result.returncode != 0:
                if self.host_config.stat_path:
                    # Try fallback to system default
                    result = self.run_ssh_command("stat --version", check=False)
                    if result.returncode != 0:
                        logger.warning(f"Both custom ({self.host_config.stat_path}) and default stat commands failed on {self.remote_host}")
                        self.stat_available = False
                        return
                    else:
                        logger.info(f"Custom stat path failed, but system stat works on {self.remote_host}")
                else:
                    logger.warning(f"Default stat command not working on {self.remote_host}")
                    self.stat_available = False
                    return
        except Exception as e:
            logger.warning(f"Remote stat command test failed: {e}")
            self.stat_available = False
            return

        if self.stat_available:
            logger.info("Stat command available on both source and destination - will use file time comparisons")
        else:
            logger.warning("Stat command not available on both systems - falling back to rsync-based comparison")

    def test_ssh_connectivity(self):
        """Test SSH connectivity to remote host before proceeding."""
        logger.info(f"Testing SSH connectivity to {self.remote_host}...")
        try:
            # Simple SSH connectivity test
            result = self.run_ssh_command("echo 'SSH connectivity OK'", capture_output=True, check=True)
            logger.info(f"SSH connectivity to {self.remote_host}: OK")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"SSH connectivity to {self.remote_host}: FAILED")
            logger.error("Please check SSH access and host availability")
            sys.exit(127)
        except Exception as e:
            logger.error(f"SSH connectivity test failed: {e}")
            sys.exit(127)

    def run_command(self, command: List[str], capture_output: bool = True, 
                   check: bool = True, dry_run_skip: bool = False) -> subprocess.CompletedProcess:
        """Run a command with proper error handling."""
        try:
            # Skip certain commands in debug mode (marked with dry_run_skip=True)
            if self.debug and dry_run_skip:
                logger.info(f"DEBUG: Would run: {' '.join(command)}")
                from unittest.mock import Mock
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
                return mock_result

            result = subprocess.run(
                command,
                capture_output=capture_output,
                text=True,
                check=check
            )
            return result
        except subprocess.CalledProcessError as e:
            if check:
                logger.error(f"Command failed: {' '.join(command)}")
                logger.error(f"Error: {e.stderr}")
                raise
            return e

    def run_ssh_command(self, command: str, capture_output: bool = True, 
                       check: bool = True) -> subprocess.CompletedProcess:
        """Run a command on the remote host via SSH."""
        ssh_cmd = [
            'ssh', '-q', 
            '-c', self.ssh_cipher,
            '-oCompression=no',
            self.remote_host,
            command
        ]
        return self.run_command(ssh_cmd, capture_output=capture_output, check=check)

    def check_remote_mount_points(self):
        """Verify remote mount points exist."""
        logger.info("Verifying remote mount points...")

        for dst_dir in self.host_config.kvm_images_dst_dirs:
            if self.host_config.skip_mount_check:
                continue  # Skip mount point check for this host type

            check_dir = os.path.dirname(dst_dir)

            try:
                result = self.run_ssh_command(f"df -hP {dst_dir}")
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    mount_point = lines[1].split()[-1]
                    if not mount_point or mount_point == "/":
                        logger.error(f"Directory {dst_dir} does not have a matching remote mount point!")
                        sys.exit(127)
                    else:
                        logger.info(f"Found remote mount point for {dst_dir}: {self.remote_host}:{mount_point}")
            except subprocess.CalledProcessError:
                logger.error(f"Failed to check mount point for {dst_dir}")
                logger.error("Cannot proceed without verified remote mount points")
                sys.exit(127)

    def parse_vm_xml(self, xml_file: str) -> Tuple[List[str], List[str]]:
        """Parse VM XML file and extract disk and NVRAM file paths."""
        disk_files = []
        nvram_files = []

        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Extract disk files
            for disk in root.findall(".//disk[@type='file']/source"):
                file_attr = disk.get('file')
                if file_attr:
                    disk_files.append(file_attr)

            # Extract NVRAM files
            for nvram in root.findall(".//nvram"):
                if nvram.text:
                    nvram_files.append(nvram.text.strip())

        except ET.ParseError as e:
            logger.warning(f"Failed to parse XML file {xml_file}: {e}")
        except FileNotFoundError:
            logger.warning(f"XML file not found: {xml_file}")

        return disk_files, nvram_files

    def get_domain_state(self, vm_name: str, remote: bool = False) -> str:
        """Get the state of a libvirt domain."""
        if self.debug:
            logger.info(f"DEBUG: Checking domain state for {vm_name} ({'remote' if remote else 'local'})")

        if remote:
            try:
                result = self.run_ssh_command(f"PATH=/bin:/opt/bin virsh domstate {vm_name}", check=False)
                if result.returncode == 0:
                    return result.stdout.strip()
            except:
                pass
        else:
            try:
                result = self.run_command(['virsh', 'domstate', vm_name], check=False)
                if result.returncode == 0:
                    return result.stdout.strip()
            except:
                pass

        return "unknown"

    def get_file_mtime(self, file_path: str, remote: bool = False) -> int:
        """Get file modification time."""
        if remote:
            if self.debug:
                logger.info(f"DEBUG: Checking remote mtime for {file_path}")

            # Use the determined stat binary path (already tested by test_stat_availability)
            stat_cmd = self.host_config.stat_path if self.host_config.stat_path else "stat"

            try:
                result = self.run_ssh_command(f"{stat_cmd} -L -c %Y {file_path}", check=False)
                if result.returncode == 0:
                    return int(result.stdout.strip())

            except Exception as e:
                logger.debug(f"Error getting remote mtime for {file_path}: {e}")

            return 0  # Assume epoch if file doesn't exist remotely or stat failed
        else:
            try:
                return int(os.path.getmtime(file_path))
            except OSError:
                return 0  # File doesn't exist locally

    def should_skip_vm(self, vm_name: str) -> bool:
        """Check if VM should be skipped due to running state."""
        if self.force_action:
            return False

        local_state = self.get_domain_state(vm_name, remote=False)
        remote_state = self.get_domain_state(vm_name, remote=True)

        if local_state == "running":
            logger.warning(f"Domain {vm_name} is running!! Skipping...")
            return True

        if remote_state == "running":
            logger.warning(f"Domain {vm_name} is running on {self.remote_host}!! Skipping...")
            return True

        return False

    def create_vxfs_snapshot(self, src_dir: str) -> Optional[Tuple[str, str, str, str]]:
        """Create VXFS snapshot if supported."""
        if not self.vxfs_snapshots or not self.host_config.vxfs_snapshots:
            return None

        try:
            # Check if vxsnap command exists
            if not shutil.which('vxsnap'):
                return None

            # Get filesystem mount point
            result = self.run_command(['df', '--output=target', src_dir])
            kvm_fs_mnt = result.stdout.strip().split('\n')[1]

            # Check if it's vxfs
            result = self.run_command(['findmnt', '-o', 'FSTYPE', kvm_fs_mnt])
            fs_type = result.stdout.strip().split('\n')[1]

            if fs_type != 'vxfs':
                return None

            # Get volume group and logical volume
            result = self.run_command(['findmnt', '-n', '-o', 'SOURCE', kvm_fs_mnt])
            source = result.stdout.strip()
            parts = source.split('/')
            if len(parts) < 6:
                return None

            vxdg = parts[4]
            vxlv = parts[5]
            vxsnap_lv = f"{vxlv}_snapshot"
            vxsnap_mnt = f"{self.vxsnap_prefix}/{vxsnap_lv}"

            if self.debug:
                logger.info(f"DEBUG: Would create VXFS snapshot for {vxdg}/{vxlv}")
                return vxdg, vxlv, vxsnap_lv, vxsnap_mnt

            logger.info(f"Creating VXFS snapshot for {vxdg}/{vxlv}...")

            # Try to prepare volume - it's OK if it's already prepared
            self.run_command(['vxsnap', '-g', vxdg, 'prepare', vxlv], check=False)

            # Try to create snapshot - it's OK if it already exists
            self.run_command(['vxsnap', '-g', vxdg, 'make', 
                             f'source={vxlv}/newvol={vxsnap_lv}/{self.vxsnap_opts}'], 
                             check=False)

            # Create mount directory
            os.makedirs(vxsnap_mnt, exist_ok=True)

            # Check if already mounted
            result = self.run_command(['findmnt', '-o', 'FSTYPE', vxsnap_mnt], check=False)
            if hasattr(result, 'returncode') and result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    fs_type = lines[1].strip()
                    if fs_type == 'vxfs':
                        logger.info(f"{vxsnap_mnt} already mounted, skipping...")
                        return vxdg, vxlv, vxsnap_lv, vxsnap_mnt

            # Try to mount snapshot - only if not already mounted
            try:
                self.run_command([
                    'mount', '-t', 'vxfs', '-o', 'ro,noatime,largefiles',
                    f'/dev/vx/dsk/{vxdg}/{vxsnap_lv}', vxsnap_mnt
                ])
            except subprocess.CalledProcessError:
                # Mount failed, but check if it's actually mounted now
                result = self.run_command(['findmnt', '-o', 'FSTYPE', vxsnap_mnt], check=False)
                if not (hasattr(result, 'returncode') and result.returncode == 0):
                    raise

            return vxdg, vxlv, vxsnap_lv, vxsnap_mnt

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create VXFS snapshot: {e}")
            return None

    def destroy_vxfs_snapshot(self, vxdg: str, vxlv: str, vxsnap_lv: str, vxsnap_mnt: str):
        """Destroy VXFS snapshot."""
        if self.debug:
            logger.info(f"DEBUG: Would destroy VXFS snapshot {vxdg}/{vxlv} at {vxsnap_mnt}")
            return

        try:
            # Check if mounted
            result = self.run_command(['findmnt', '-o', 'FSTYPE', vxsnap_mnt], check=False)
            if hasattr(result, 'returncode') and result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    fs_type = lines[1].strip()
                    if fs_type == 'vxfs':
                        # Unmount
                        result = self.run_command(['umount', vxsnap_mnt], check=False)
                        if hasattr(result, 'returncode') and result.returncode == 0:
                            logger.info(f"Destroying VXFS snapshot for {vxdg}/{vxlv}...")
                            self.run_command(['vxsnap', '-g', vxdg, 'dis', vxsnap_lv])
                            self.run_command(['vxedit', '-g', vxdg, '-fr', 'rm', vxsnap_lv])
                            self.run_command(['vxsnap', '-g', vxdg, 'unprepare', vxlv])
                        else:
                            logger.warning(f"Failed umounting {vxsnap_mnt}, skipping snapshot deletion...")
                # If not mounted or not vxfs, no cleanup needed
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to destroy VXFS snapshot: {e}")
        except Exception as e:
            logger.warning(f"Error checking snapshot mount status: {e}")

    def sync_file(self, src_file: str, dst_dir: str) -> bool:
        """Sync a single file using rsync."""
        rsync_cmd = ['rsync']

        # Add rsync options
        rsync_cmd.extend(self.rsync_options.split())

        # Add host-specific rsync path
        if self.host_config.rsync_path:
            rsync_cmd.extend(['--rsync-path', self.host_config.rsync_path])

        # Add checksum option if needed
        if self.force_checksum:
            rsync_cmd.append('-c')

        # Add update option if needed
        if self.update_only:
            rsync_cmd.append('-u')

        # Add test option if needed
        if self.test_only:
            rsync_cmd.append('-n')

        # Add dry-run flag in debug mode
        if self.debug:
            rsync_cmd.append('--dry-run')

        # Add source and destination
        rsync_cmd.append(src_file)
        rsync_cmd.append(f"{self.remote_host}:{dst_dir}/")

        try:
            # Use parent's stdout/stderr so we can see rsync progress
            process = subprocess.Popen(rsync_cmd, stdout=None, stderr=None)

            # Track this process for cleanup
            self.child_processes.append(process)

            # Wait for completion
            returncode = process.wait()

            # Remove from tracking list when done
            if process in self.child_processes:
                self.child_processes.remove(process)

            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, rsync_cmd)

            return True
        except KeyboardInterrupt:
            logger.warning("Interrupted by user during file sync")
            # Clean up child processes
            self.cleanup_child_processes()
            raise
        except subprocess.CalledProcessError:
            if not self.debug:
                logger.error(f"Failed to sync {src_file}")
            return False

    def sync_files_parallel(self, file_list: List[str], dst_dir: str) -> bool:
        """Sync multiple files using parallel rsync processes like the original bash script."""
        if not file_list:
            return True

        # Build base rsync command
        rsync_cmd = ['rsync']
        rsync_cmd.extend(self.rsync_options.split())

        # Add host-specific rsync path
        if self.host_config.rsync_path:
            rsync_cmd.extend(['--rsync-path', self.host_config.rsync_path])

        # Add checksum option if needed
        if self.force_checksum:
            rsync_cmd.append('-c')

        # Add update option if needed
        if self.update_only:
            rsync_cmd.append('-u')

        # Add test option if needed
        if self.test_only:
            rsync_cmd.append('-n')

        # Add dry-run flag in debug mode
        if self.debug:
            rsync_cmd.append('--dry-run')
            logger.info(f"DEBUG: Running parallel rsync with {self.host_config.threads} threads (dry-run mode)...")

        # Use xargs to parallelize exactly like the original bash script:
        # echo ${DISK_LIST}| xargs -n1 |
        # xargs --replace -n1 -I% -P${NR_THREADS} rsync ${RSYNC_OPTIONS} % ${REMOTE_HOST}:${dst_dir}

        try:
            # Build the command exactly like the bash version
            files_str = ' '.join(file_list)
            rsync_cmd_str = ' '.join(rsync_cmd)

            # Use bash to execute the same xargs pipeline as the original
            bash_cmd = (
                f'echo "{files_str}" | xargs -n1 | '
                f'xargs --replace -n1 -I% -P{self.host_config.threads} '
                f'{rsync_cmd_str} % {self.remote_host}:{dst_dir}/'
            )

            # Debug output to see the exact command
            if not self.debug:
                logger.debug(f"Executing parallel rsync: {bash_cmd}")
                import sys
                sys.stdout.flush()  # Ensure output is flushed

            # Execute with direct stdout/stderr so we can see rsync progress
            process = subprocess.Popen(
                ['bash', '-c', bash_cmd],
                stdout=None,  # Use parent's stdout
                stderr=None   # Use parent's stderr
            )

            # Track this process for cleanup
            self.child_processes.append(process)

            # Wait for completion
            returncode = process.wait()

            # Remove from tracking list when done
            if process in self.child_processes:
                self.child_processes.remove(process)

            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, bash_cmd)

            return True

        except KeyboardInterrupt:
            logger.warning("Interrupted by user during parallel rsync")
            # Clean up only our child processes, not all rsync processes
            self.cleanup_child_processes()
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to sync files in parallel: {e}")
            return False

    def process_vm_list(self, vm_list: List[str]) -> List[str]:
        """Process and validate VM list."""
        validated_vms = []

        if not vm_list:
            vm_list = self.host_config.default_vm_list.split()

        for vm in vm_list:
            vm = vm.replace('.xml', '')  # Remove .xml extension if present
            xml_file = f"{self.kvm_conf_src_dir}/{vm}.xml"

            if os.path.exists(xml_file):
                logger.info(f"Found Domain: {vm} ({xml_file})")
                validated_vms.append(vm)

        return sorted(set(validated_vms))

    def sync_vm_configs(self, vm_list: List[str]) -> bool:
        """Sync VM configuration files."""
        if not vm_list:
            return True

        logger.info(f"Waiting {self.wait_time} seconds before push to {self.remote_host}...")
        if not self.debug:
            time.sleep(self.wait_time)

        success = True
        for vm in vm_list:
            # Check for templated XML
            template_xml = f"{self.default_kvm_templates}/{vm}.xml"
            if os.path.exists(template_xml):
                xml_src = template_xml
            else:
                xml_src = f"{self.kvm_conf_src_dir}/{vm}.xml"

            # Sync XML file (with quiet flag to reduce output noise for small files)
            rsync_cmd = ['rsync', '-q']  # Quiet mode for XML files
            rsync_cmd.extend(self.rsync_options.split())
            if self.host_config.rsync_path:
                rsync_cmd.extend(['--rsync-path', self.host_config.rsync_path])

            # Add dry-run flag in debug mode
            if self.debug:
                rsync_cmd.append('--dry-run')

            rsync_cmd.extend([xml_src, f"{self.remote_host}:{self.kvm_conf_dst_dir}/{vm}.xml"])

            try:
                process = subprocess.Popen(rsync_cmd)

                # Track this process for cleanup
                self.child_processes.append(process)

                # Wait for completion
                returncode = process.wait()

                # Remove from tracking list when done
                if process in self.child_processes:
                    self.child_processes.remove(process)

                if returncode != 0:
                    raise subprocess.CalledProcessError(returncode, rsync_cmd)

            except KeyboardInterrupt:
                logger.warning("Interrupted by user during XML sync")
                self.cleanup_child_processes()
                raise
            except subprocess.CalledProcessError:
                if not self.debug:
                    logger.error(f"Failed to sync {xml_src}")
                    success = False
                    continue

            # Handle domain definition
            if not self.host_config.skip_define:
                if self.debug:
                    logger.info(f"DEBUG: Would define domain {vm} on {self.remote_host}")
                else:
                    try:
                        # Check if remote XML exists
                        remote_xml = f"{self.kvm_conf_dst_dir}/{vm}.xml"
                        result = self.run_ssh_command(f"PATH=/bin:/opt/bin test -f {remote_xml}", check=False)

                        if result.returncode != 0:
                            logger.info(f"Remote XML does not exist, copying {xml_src}...")
                            if not self.sync_file(xml_src, self.kvm_conf_dst_dir):
                                success = False
                                continue

                        # Edit remote file to fix machine types
                        sed_cmd = (
                            f"sed -i "
                            f"-e 's@pc-i440fx-rhel7.6.0@pc@' "
                            f"-e 's@pc-q35-rhel[789].[0-9].0@q35@' "
                            f"{remote_xml}"
                        )
                        self.run_ssh_command(sed_cmd)

                        # Define guest on remote machine
                        self.run_ssh_command(f"PATH=/bin:/opt/bin virsh define {remote_xml}")

                    except subprocess.CalledProcessError as e:
                        logger.error(f"Failed to define domain {vm}: {e}")
                        success = False

        return success

    def main(self):
        """Main execution function."""
        # Parse command line arguments first (allows --version to work without root)
        parser = argparse.ArgumentParser(
            description='Replicate KVM virtual machines to remote hypervisor',
            formatter_class=argparse.RawDescriptionHelpFormatter
        )

        parser.add_argument('-c', '--checksum', action='store_true',
                           help='Force checksumming')
        parser.add_argument('-d', '--debug', action='store_true',
                           help='Debug mode - show commands without executing')
        parser.add_argument('-f', '--force', action='store_true',
                           help='Overwrite even if files are more recent on destination')
        parser.add_argument('-p', '--poweroff', action='store_true',
                           help='Power off remote system when sync is done')
        parser.add_argument('-s', '--novxsnap', action='store_true',
                           help="Don't use vxfs snapshots even if supported")
        parser.add_argument('-t', '--test', action='store_true',
                           help="Don't copy, only perform a check test")
        parser.add_argument('-u', '--update', action='store_true',
                           help='Only update if newer files')
        parser.add_argument('--host', '--dest-host', dest='host', 
                           help='Override destination host (default: auto-detect from script name)')
        parser.add_argument('-V', '--version', action='version',
                           version=__version__,
                           help='Show version information and exit')
        parser.add_argument('vm_list', nargs='*',
                           help='List of VMs to replicate (default: all configured VMs)')

        args = parser.parse_args()

        # Check if running as root (after parsing args so --version works)
        if os.getuid() != 0:
            logger.error("This script must be run as root")
            logger.error("Please run: sudo " + " ".join(sys.argv))
            sys.exit(1)

        # Set options from arguments
        self.force_checksum = args.checksum
        self.debug = args.debug
        self.force_action = args.force
        self.poweroff = args.poweroff
        self.vxfs_snapshots = not args.novxsnap
        self.test_only = args.test
        self.update_only = args.update

        # Determine remote host: CLI override or auto-detect from script name
        if args.host:
            remote_host = self.validate_remote_host(args.host)
            logger.info(f"Using CLI-specified destination host: {remote_host}")
        else:
            remote_host = self.get_remote_host_from_script_name()
            logger.info(f"Auto-detected destination host from script name: {remote_host}")

        self.setup_host_config(remote_host)

        # Test SSH connectivity first (fail fast) - critical even in debug mode
        self.test_ssh_connectivity()

        # Test stat availability on both systems (unless we're skipping stat checks)
        if not self.host_config.skip_stat_check:
            self.test_stat_availability()

        # Check remote mount points
        self.check_remote_mount_points()

        # Process VM list
        vm_list = self.process_vm_list(args.vm_list)
        logger.info(f"Final VM List: {' '.join(vm_list)}")

        if not vm_list:
            logger.warning("No VMs to process")
            return 0

        # Main processing loop
        success = True
        for i, src_dir in enumerate(self.kvm_images_src_dirs):
            if not os.path.isdir(src_dir):
                logger.warning(f"VM Directory: {src_dir} not found!")
                continue

            # Lists to track files to sync
            vms_to_sync = []
            disk_files = []
            nvram_files = []
            snapshot_info = None

            try:
                # Process each VM
                for vm in vm_list:
                    if self.should_skip_vm(vm):
                        continue

                    xml_file = f"{self.kvm_conf_src_dir}/{vm}.xml"
                    if not os.path.exists(xml_file):
                        continue

                    # Parse VM XML to get disk and NVRAM files
                    vm_disks, vm_nvrams = self.parse_vm_xml(xml_file)

                    vm_needs_sync = False

                    # Check disk files
                    for disk_file in vm_disks:
                        if not os.path.exists(disk_file):
                            continue

                        dst_file = f"{self.host_config.kvm_images_dst_dirs[i]}/{os.path.basename(disk_file)}"

                        if self.force_action or self.host_config.skip_stat_check or not self.stat_available:
                            if not self.stat_available and not self.host_config.skip_stat_check:
                                logger.debug(f"Stat not available on both systems, syncing {disk_file}")
                            logger.info(f"*** Will rsync ({vm}) {disk_file} to {self.remote_host}:{self.host_config.kvm_images_dst_dirs[i]}")
                            disk_files.append(disk_file)
                            vm_needs_sync = True
                        else:
                            local_mtime = self.get_file_mtime(disk_file)
                            remote_mtime = self.get_file_mtime(dst_file, remote=True)

                            if local_mtime > remote_mtime:
                                logger.info(f"*** Will rsync ({vm}) {disk_file} to {self.remote_host}:{self.host_config.kvm_images_dst_dirs[i]}")
                                disk_files.append(disk_file)
                                vm_needs_sync = True
                            elif local_mtime == remote_mtime:
                                logger.info(f"stat() times on {vm} ({disk_file}) are identical, skipping...")

                    # Check NVRAM files
                    for nvram_file in vm_nvrams:
                        if not os.path.exists(nvram_file):
                            continue

                        dst_file = f"{self.host_config.kvm_nvram_dst_dirs[i]}/{os.path.basename(nvram_file)}"

                        if self.force_action or self.host_config.skip_stat_check or not self.stat_available:
                            if not self.stat_available and not self.host_config.skip_stat_check:
                                logger.debug(f"Stat not available on both systems, syncing {nvram_file}")
                            logger.info(f"*** Will rsync ({vm}) {nvram_file} to {self.remote_host}:{self.host_config.kvm_nvram_dst_dirs[i]}")
                            nvram_files.append(nvram_file)
                            vm_needs_sync = True
                        else:
                            local_mtime = self.get_file_mtime(nvram_file)
                            remote_mtime = self.get_file_mtime(dst_file, remote=True)

                            if local_mtime > remote_mtime:
                                logger.info(f"*** Will rsync ({vm}) {nvram_file} to {self.remote_host}:{self.host_config.kvm_nvram_dst_dirs[i]}")
                                nvram_files.append(nvram_file)
                                vm_needs_sync = True
                            elif local_mtime == remote_mtime:
                                logger.info(f"stat() times on {vm} ({nvram_file}) are identical, skipping...")

                    if vm_needs_sync:
                        vms_to_sync.append(vm)

                logger.info(f"Final VM List: {' '.join(vms_to_sync)}")
                if disk_files:
                    logger.info(f"Final Disk List: {' '.join(disk_files)}")
                if nvram_files:
                    logger.info(f"Final NVRAM List: {' '.join(nvram_files)}")

                # Sync VM configurations
                if vms_to_sync:
                    if not self.sync_vm_configs(vms_to_sync):
                        success = False

                # Create snapshot if needed
                if (disk_files or nvram_files) and self.vxfs_snapshots:
                    snapshot_info = self.create_vxfs_snapshot(src_dir)
                    if snapshot_info:
                        # Update file paths to use snapshot
                        vxdg, vxlv, vxsnap_lv, vxsnap_mnt = snapshot_info
                        if self.debug:
                            logger.info(f"DEBUG: Would update file paths to use snapshot mount {vxsnap_mnt}")
                        else:
                            kvm_fs_mnt = self.run_command(['df', '--output=target', src_dir]).stdout.strip().split('\n')[1]
                            disk_files = [f.replace(kvm_fs_mnt, vxsnap_mnt) for f in disk_files]
                            nvram_files = [f.replace(kvm_fs_mnt, vxsnap_mnt) for f in nvram_files]

                # Sync disk files with parallel rsync processes
                if disk_files:
                    logger.info(f"Starting parallel rsync for {len(disk_files)} disk files...")
                    if not self.sync_files_parallel(disk_files, self.host_config.kvm_images_dst_dirs[i]):
                        success = False

                # Sync NVRAM files
                if nvram_files:
                    for nvram_file in nvram_files:
                        if not self.sync_file(nvram_file, self.host_config.kvm_nvram_dst_dirs[i]):
                            success = False

                # Copy tools (entire script directory like the bash version)
                script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))  # Get directory of executed script
                dst_dir = os.path.dirname(self.host_config.kvm_images_dst_dirs[i])
                logger.info(f"Copying tools to {self.remote_host}:{dst_dir}...")
                if self.debug:
                    logger.info(f"DEBUG: Would copy {script_dir} to {self.remote_host}:{dst_dir}")
                else:
                    rsync_cmd = ['rsync']
                    rsync_cmd.extend(self.rsync_options.split())
                    if self.host_config.rsync_path:
                        rsync_cmd.extend(['--rsync-path', self.host_config.rsync_path])
                    rsync_cmd.extend([script_dir, f"{self.remote_host}:{dst_dir}/"])

                    try:
                        process = subprocess.Popen(rsync_cmd)

                        # Track this process for cleanup
                        self.child_processes.append(process)

                        # Wait for completion
                        returncode = process.wait()

                        # Remove from tracking list when done
                        if process in self.child_processes:
                            self.child_processes.remove(process)

                        if returncode != 0:
                            raise subprocess.CalledProcessError(returncode, rsync_cmd)

                    except KeyboardInterrupt:
                        logger.warning("Interrupted by user during tools copy")
                        self.cleanup_child_processes()
                        raise
                    except subprocess.CalledProcessError:
                        logger.error(f"Failed to copy tools to {self.remote_host}:{dst_dir}")
                        success = False

            finally:
                # Clean up snapshot
                if snapshot_info:
                    vxdg, vxlv, vxsnap_lv, vxsnap_mnt = snapshot_info
                    self.destroy_vxfs_snapshot(vxdg, vxlv, vxsnap_lv, vxsnap_mnt)

        # Handle poweroff option
        if self.poweroff:
            if self.debug:
                logger.info(f"DEBUG: Would run hastop -local on remote host {self.remote_host}")
                logger.info(f"DEBUG: Would run /sbin/poweroff on remote host {self.remote_host}")
            else:
                time.sleep(1.0)
                logger.info(f"Running hastop -local on remote host {self.remote_host}")
                try:
                    self.run_ssh_command("sync;/opt/VRTSvcs/bin/hastop -local 2>/dev/null", check=False)
                except:
                    pass

                logger.info(f"Running /sbin/poweroff on remote host {self.remote_host}")
                try:
                    self.run_ssh_command("sync;/sbin/poweroff", check=False)
                except:
                    pass

        return 0 if success else 1


if __name__ == '__main__':
    replicator = None
    try:
        replicator = KVMReplicator()
        sys.exit(replicator.main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        # Clean up only our child processes, not all rsync processes
        if replicator:
            replicator.cleanup_child_processes()
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
