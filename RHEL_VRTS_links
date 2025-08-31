#!/usr/bin/env python3
"""
RHEL Veritas Storage Foundation Kernel Module Relinker

Python rewrite of RHEL_VRTS_links bash script
Maintains same command syntax: --force, --silent, --exec
"""

# $Id: RHEL_VRTS_links,v 1.03 2025/08/31 21:00:00 dynamic-vcs-patterns Exp $
__version__ = "RHEL_VRTS_links,v 1.03 2025/08/31 21:00:00 dynamic-vcs-patterns Exp"

#
# VERSION HISTORY:
# ================
#
# v1.03 (2025-08-31): Dynamic VCS module patterns using detected RHEL version
#   - Replaced hardcoded pattern ranges el[7-9] and el1[0-9] with dynamic patterns
#   - VCS module patterns now use detected RHEL version (e.g., el9 for RHEL 9)
#   - Reduced pattern list from 24 to 12 targeted patterns for better performance
#   - Automatically correct for any RHEL major version without code changes
#   - Enhanced debug output shows which RHEL version is used for pattern generation
#
# v1.02 (2025-08-31): Enhanced VCS module patterns and command paths
#   - Enhanced VCS module patterns to support future RHEL versions (el10+)
#   - Added el1[0-9] patterns for RHEL 10-19 future compatibility
#   - Updated all system commands to use full paths (/usr/sbin/semanage)
#   - Improved robustness against shell aliases and environment variations
#   - Maintained backward compatibility with current RHEL 7-9 versions
#
# v1.01 (2025-08-31): Enhanced version detection and display
#   - Robust RHEL version detection with multiple fallback methods
#   - Enhanced --version flag now displays detected RHEL version
#   - Support for /etc/redhat-release, /etc/os-release, /etc/system-release fallbacks
#   - Changed default RHEL version from 8 to 9 (more current)
#   - Debug mode support for version detection (--version --debug)
#   - Updated descriptions to use "relinker" terminology
#   - Production tested on daltigoth and solinari RHEL 9.6 systems
#
# v1.00 (2025-08-31): Python rewrite with enhanced features
#   - Complete Python rewrite of original bash script
#   - Added --version flag with proper version history
#   - Enhanced debug mode with detailed matching logic
#   - Improved kernel module blacklisting with cleaner set-based approach
#   - Added comprehensive error handling and logging
#   - Maintained full compatibility with original command syntax
#   - Added /dev/null symlink detection for disabled VCS modules
#   - Enhanced SELinux context handling
#   - Improved RHEL version detection and multi-kernel processing
#
# PRIOR VERSIONS: Legacy bash script
#   - Original bash implementation
#   - Basic kernel module linking functionality
#   - Support for VxFS, VxVM, and VCS modules
#   - SELinux context management
#

import os
import sys
import re
import subprocess
import glob
import argparse
from pathlib import Path
from typing import List, Optional

class VRTSLinker:
    def __init__(self):
        self.force = False
        self.silent = False
        self.run_exec = False
        self.debug = False
        self.action = 0
        self.rhel_version = None

        # Module groups and their target directories
        self.generic_modules = ['veki', 'vxglm', 'vxgms', 'vxodm', 'storageapi']
        self.vxfs_modules = ['vxfs', 'fdd', 'vxportal', 'vxcafs']
        # VxVM modules split into allowed and blacklisted (as per updated shell script)
        self.vxvm_allowed_modules = [
            'vxdmp', 'vxio', 'vxspec',
            'dmpaaa', 'dmpaa', 'dmpalua', 'dmpapf', 'dmpapg', 'dmpap',
            'dmpCLARiiON', 'dmpEngenio', 'dmphuawei', 'dmpvmax',
            'dmpinv', 'dmpjbod', 'dmpnalsi', 'dmpnvme', 'dmpsun7x10alua', 'dmpsvc'
        ]
        # Blacklisted modules that should be REMOVED if they exist
        self.vxvm_blacklisted_modules = [
            'dmpkove'  # Excluded due to: dmpkove.ko needs unknown symbol kdsa_ext_ioctl
        ]
        self.vcs_modules = ['gab', 'llt', 'vxfen', 'amf']
        self.vcsmm_modules = ['vcsmm']

        # SELinux module directories to label
        self.selinux_dirs = [
            '/etc/vx/kernel',
            '/opt/VRTSamf/modules',
            '/opt/VRTSgab/modules',
            '/opt/VRTSllt/modules',
            '/opt/VRTSvxfen/modules',
            '/opt/VRTSvcs/rac/modules'
        ]

        # Modules to load automatically
        self.auto_load_modules = ['vxspec', 'vxio', 'fdd', 'vxportal', 'vxdmp']

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def myecho(self, command: str):
        """Execute command or print it based on run_exec flag"""
        if not self.run_exec:
            print(command)
        else:
            try:
                result = subprocess.run(command, shell=True, check=False,
                                      capture_output=False)

                # Handle specific commands with acceptable non-zero exit codes
                if "dracut" in command and result.returncode == 3:
                    self.debug_print(f"dracut returned exit code 3 (no work needed) - this is OK")
                    return True
                elif result.returncode != 0:
                    print(f"Command failed with exit code {result.returncode}: {command}")
                    return False
                else:
                    return True

            except Exception as e:
                print(f"Error executing command: {command} - {e}")
                return False

    def check_root_privileges(self):
        """Check if running as root, exec sudo if not"""
        if os.getuid() != 0:
            # Re-exec with sudo
            sudo_cmd = ['sudo'] + sys.argv
            os.execvp('sudo', sudo_cmd)

    def check_rhel_system(self):
        """Check if running on Red Hat Enterprise Linux"""
        try:
            with open('/etc/redhat-release', 'r') as f:
                content = f.read().strip()

            if 'Red Hat Enterprise Linux' not in content:
                print(f"ERROR: This script is designed for Red Hat Enterprise Linux only.")
                print(f"Detected system: {content}")
                print(f"This script requires RHEL to function properly with Veritas Storage Foundation.")
                sys.exit(1)

            self.debug_print(f"Verified RHEL system: {content}")

        except FileNotFoundError:
            print("ERROR: /etc/redhat-release not found.")
            print("This script is designed for Red Hat Enterprise Linux only.")
            print("This script requires RHEL to function properly with Veritas Storage Foundation.")
            sys.exit(1)
        except Exception as e:
            print(f"ERROR: Could not verify RHEL system: {e}")
            print("This script requires RHEL to function properly with Veritas Storage Foundation.")
            sys.exit(1)

    def get_rhel_version(self):
        """Get RHEL version using multiple detection methods"""

        # Method 1: Try lsb_release (if available)
        try:
            result = subprocess.run(['lsb_release', '-r'],
                                  capture_output=True, text=True, check=True)
            # Extract version number (e.g., "Release: 8.5" -> "8")
            version_line = result.stdout.strip()
            version = version_line.split(':')[1].strip().split('.')[0]
            rhel_version = int(version)
            self.debug_print(f"Detected RHEL version via lsb_release: {rhel_version}")
            return rhel_version
        except (subprocess.CalledProcessError, ValueError, IndexError, FileNotFoundError):
            self.debug_print("lsb_release not available or failed, trying alternative methods")

        # Method 2: Try /etc/redhat-release
        try:
            with open('/etc/redhat-release', 'r') as f:
                content = f.read().strip()
            # Look for version pattern like "Red Hat Enterprise Linux release 9.1" or "CentOS Linux release 8.4"
            match = re.search(r'release\s+(\d+)', content)
            if match:
                rhel_version = int(match.group(1))
                self.debug_print(f"Detected RHEL version via /etc/redhat-release: {rhel_version}")
                return rhel_version
        except (FileNotFoundError, ValueError, AttributeError):
            self.debug_print("/etc/redhat-release not available or invalid format")

        # Method 3: Try /etc/os-release
        try:
            with open('/etc/os-release', 'r') as f:
                content = f.read()
            # Look for VERSION_ID="9.1" or similar
            match = re.search(r'VERSION_ID="(\d+)', content)
            if match:
                rhel_version = int(match.group(1))
                self.debug_print(f"Detected RHEL version via /etc/os-release: {rhel_version}")
                return rhel_version
        except (FileNotFoundError, ValueError, AttributeError):
            self.debug_print("/etc/os-release not available or invalid format")

        # Method 4: Try /etc/system-release
        try:
            with open('/etc/system-release', 'r') as f:
                content = f.read().strip()
            # Look for version pattern similar to redhat-release
            match = re.search(r'release\s+(\d+)', content)
            if match:
                rhel_version = int(match.group(1))
                self.debug_print(f"Detected RHEL version via /etc/system-release: {rhel_version}")
                return rhel_version
        except (FileNotFoundError, ValueError, AttributeError):
            self.debug_print("/etc/system-release not available or invalid format")

        # Fallback: Default to RHEL 9
        print("Warning: Could not determine RHEL version using any method")
        self.debug_print("Failed to detect RHEL version, defaulting to 9")
        return 9  # Default to 9

    def get_installed_kernels(self) -> List[str]:
        """Get list of installed kernel versions from RPM"""
        try:
            result = subprocess.run(['rpm', '-q', 'kernel'],
                                  capture_output=True, text=True, check=True)
            kernels = []
            for line in result.stdout.strip().split('\n'):
                if line.startswith('kernel-'):
                    # Extract version from "kernel-4.18.0-348.el8.x86_64"
                    kernel_version = line[7:]  # Remove "kernel-" prefix
                    kernels.append(kernel_version)

            self.debug_print(f"Found {len(kernels)} installed kernels:")
            for kernel in kernels:
                self.debug_print(f"  - {kernel}")
            return kernels
        except subprocess.CalledProcessError:
            print("Error: Could not query installed kernels")
            self.debug_print("Failed to query RPM for installed kernels")
            return []

    def get_blacklist_subrevs(self, kernel_version: str) -> set:
        """Get set of subrevision numbers to blacklist for kernel modules

        This replicates the bash logic but returns a clean set instead of a regex pattern
        """
        self.debug_print(f"Analyzing blacklist logic for kernel: {kernel_version}")
        try:
            # Get all available subrevisions from /etc/vx/kernel
            kmod_dir = Path('/etc/vx/kernel')
            if not kmod_dir.exists():
                self.debug_print("/etc/vx/kernel directory not found, no blacklisting")
                return set()

            ko_files = list(kmod_dir.glob('*.ko.*'))
            all_subrevs = set()
            self.debug_print(f"Found {len(ko_files)} .ko files in /etc/vx/kernel")

            for ko_file in ko_files:
                # Extract version part after .ko.
                # e.g., "vxfs.ko.5.14.0-284.11.1.el9_2.x86_64" -> "5.14.0-284.11.1.el9_2.x86_64"
                name_parts = ko_file.name.split('.ko.')
                if len(name_parts) >= 2:
                    version_part = name_parts[1]  # e.g., "5.14.0-284.11.1.el9_2.x86_64"
                    if '-' in version_part:
                        # Cut by dash and take second field (index 1)
                        dash_parts = version_part.split('-')
                        if len(dash_parts) >= 2:
                            subrev_part = dash_parts[1]  # e.g., "284.11.1.el9_2.x86_64"
                            # Cut by dot and take first field (the subrevision number)
                            dot_parts = subrev_part.split('.')
                            if dot_parts:
                                try:
                                    subrev = int(dot_parts[0])  # e.g., 284
                                    all_subrevs.add(subrev)
                                except ValueError:
                                    continue

            # Get current kernel's subrevision
            # For kernel_version like "5.14.0-284.11.1.el9_2.x86_64"
            kernel_parts = kernel_version.split('.')
            if len(kernel_parts) >= 3:
                # Take first 3 parts: "5.14.0-284"
                first_three = '.'.join(kernel_parts[:3])
                if '-' in first_three:
                    dash_parts = first_three.split('-')
                    if len(dash_parts) >= 2:
                        try:
                            my_subrev = int(dash_parts[1])  # e.g., 284
                            self.debug_print(f"Current kernel subrevision: {my_subrev}")
                        except ValueError:
                            self.debug_print("Could not parse current kernel subrevision")
                            return set()
                    else:
                        self.debug_print("No subrevision found in current kernel version")
                        return set()
                else:
                    self.debug_print("No dash found in current kernel version")
                    return set()
            else:
                self.debug_print("Invalid kernel version format")
                return set()

            # Find all available subrevisions
            self.debug_print(f"All available subrevisions: {sorted(all_subrevs)}")

            # Return subrevisions greater than current (these should be blacklisted)
            blacklist = {subrev for subrev in all_subrevs if subrev > my_subrev}
            if blacklist:
                self.debug_print(f"Blacklisted subrevisions (newer than {my_subrev}): {sorted(blacklist)}")
            else:
                self.debug_print("No subrevisions to blacklist (none newer than current)")
            return blacklist

        except Exception as e:
            self.debug_print(f"Exception in blacklist analysis: {e}")
            return set()

    def find_best_module(self, module_name: str, kernel_version: str,
                        kmod_dir: str, blacklist_subrevs: set) -> Optional[str]:
        """Find the best matching kernel module for given kernel version

        This replicates the bash logic but uses a clean set-based blacklist approach
        """
        self.debug_print(f"Finding best module for {module_name} with kernel {kernel_version}")
        krev = kernel_version.split('-')[0]  # e.g., "5.14.0"
        ksubrev = '.'.join(kernel_version.split('.')[:3])  # e.g., "5.14.0"
        self.debug_print(f"  KREV (base): {krev}, KSUBREV (full): {ksubrev}")

        # Try with full subrevision first (KSUBREV), then with base revision (KREV)
        patterns = [
            f"{kmod_dir}/{module_name}.ko.{ksubrev}.*",
            f"{kmod_dir}/{module_name}.ko.{krev}-*"
        ]

        for i, pattern in enumerate(patterns):
            pattern_type = "KSUBREV" if i == 0 else "KREV"
            self.debug_print(f"  Trying pattern {i+1}/2 ({pattern_type}): {pattern}")
            files = glob.glob(pattern)
            self.debug_print(f"    Found {len(files)} matching files")

            if files:
                # Sort by version using a version-aware sort (similar to sort -V)
                def version_key(filename):
                    # Extract version from filename like "module.ko.5.14.0-284.11.1.el9_2.x86_64"
                    parts = filename.split('.ko.')
                    if len(parts) >= 2:
                        version = parts[1]
                        # Split by dash to get the subrevision for sorting
                        if '-' in version:
                            dash_parts = version.split('-')
                            if len(dash_parts) >= 2:
                                try:
                                    # Use the numeric part for sorting
                                    subrev = int(dash_parts[1].split('.')[0])
                                    return subrev
                                except ValueError:
                                    return 0
                    return 0

                files.sort(key=version_key)
                if self.debug:
                    self.debug_print(f"    Files sorted by version:")
                    for f in files:
                        subrev = version_key(f)
                        self.debug_print(f"      {f} (subrev: {subrev})")

                # Filter out blacklisted versions - much cleaner than regex matching
                original_count = len(files)
                if blacklist_subrevs:
                    filtered_files = []
                    for f in files:
                        file_subrev = version_key(f)  # Extract subrev from filename
                        if file_subrev not in blacklist_subrevs:
                            filtered_files.append(f)
                        else:
                            self.debug_print(f"      FILTERED OUT: {f} (subrev {file_subrev} is blacklisted)")
                    files = filtered_files
                    self.debug_print(f"    After blacklist filtering: {len(files)}/{original_count} files remaining")

                if files:
                    selected = files[-1]  # Return the latest version (tail -1 equivalent)
                    self.debug_print(f"    SELECTED: {selected}")
                    return selected
                else:
                    self.debug_print(f"    No files remaining after filtering")

        self.debug_print(f"  No suitable module found for {module_name}")
        return None

    def create_directory_if_needed(self, directory: str):
        """Create directory if it doesn't exist"""
        if not os.path.exists(directory):
            self.myecho(f"/bin/mkdir -p {directory}")

    def process_generic_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process generic Veritas modules"""
        kmod_dir = '/etc/vx/kernel'

        for module in self.generic_modules:
            srcmod = self.find_best_module(module, kernel_version, kmod_dir, blacklist_subrevs)
            if srcmod:
                target_dir = f"{top_dir}/veritas/{module}"
                target_file = f"{target_dir}/{module}.ko"

                self.create_directory_if_needed(target_dir)

                if self.force or not os.path.exists(target_file) or os.path.getsize(target_file) == 0:
                    self.myecho(f"/bin/ln -sf {srcmod} {target_file}")
                    self.action += 1

    def process_vxfs_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process VxFS modules"""
        kmod_dir = '/etc/vx/kernel'
        target_dir = f"{top_dir}/veritas/vxfs"

        self.create_directory_if_needed(target_dir)

        for module in self.vxfs_modules:
            srcmod = self.find_best_module(module, kernel_version, kmod_dir, blacklist_subrevs)
            if srcmod:
                target_file = f"{target_dir}/{module}.ko"

                if self.force or not os.path.exists(target_file) or os.path.getsize(target_file) == 0:
                    self.myecho(f"/bin/ln -sf {srcmod} {target_file}")
                    self.action += 1

    def process_vxvm_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process VxVM allowed modules"""
        kmod_dir = '/etc/vx/kernel'
        target_dir = f"{top_dir}/veritas/vxvm"

        self.create_directory_if_needed(target_dir)

        # Process allowed modules
        for module in self.vxvm_allowed_modules:
            srcmod = self.find_best_module(module, kernel_version, kmod_dir, blacklist_subrevs)
            if srcmod:
                target_file = f"{target_dir}/{module}.ko"

                if self.force or not os.path.exists(target_file) or os.path.getsize(target_file) == 0:
                    self.myecho(f"/bin/ln -sf {srcmod} {target_file}")
                    self.action += 1

    def process_vxvm_blacklisted_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process VxVM blacklisted modules - REMOVE them if they exist"""
        kmod_dir = '/etc/vx/kernel'
        target_dir = f"{top_dir}/veritas/vxvm"

        self.debug_print("Processing blacklisted VxVM modules (for removal)...")

        # Process blacklisted modules - these should be REMOVED
        for module in self.vxvm_blacklisted_modules:
            srcmod = self.find_best_module(module, kernel_version, kmod_dir, blacklist_subrevs)
            if srcmod:
                target_file = f"{target_dir}/{module}.ko"

                if self.force or (os.path.exists(target_file) and os.path.getsize(target_file) > 0):
                    if os.path.exists(target_file):
                        self.debug_print(f"Removing blacklisted module: {target_file}")
                        self.myecho(f"/bin/rm -fv {target_file}")
                        self.action += 1
                    else:
                        self.debug_print(f"Blacklisted module {target_file} already absent")

    def find_vcs_module(self, module_name: str, kernel_version: str,
                       local_kmod_dir: str, blacklist_subrevs: set) -> Optional[str]:
        """Find VCS module with complex pattern matching (updated with RDMA support and /dev/null detection)"""
        krev = kernel_version.split('-')[0]
        ksubrev = '.'.join(kernel_version.split('.')[:3])

        self.debug_print(f"  Looking for VCS module {module_name} in {local_kmod_dir}")

        # Use detected RHEL version to build targeted patterns (much cleaner than hardcoded ranges)
        rhel_version = self.rhel_version  # Already detected in run()
        self.debug_print(f"  Using RHEL version {rhel_version} for VCS module patterns")
        
        patterns = [
            # KSUBREV patterns (prioritizing non-RDMA first, then RDMA)
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}_[0-9]*.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}_[0-9]*.x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}_[0-9]*.x86_64-rdma",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}.x86_64-nonrdma", 
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*.el{rhel_version}.x86_64-rdma",
            # KREV patterns
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}_[0-9]*.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}_[0-9]*.x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}_[0-9]*.x86_64-rdma",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}.x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*.el{rhel_version}.x86_64-rdma"
        ]

        def extract_vcs_subrev(filename):
            """Extract subrevision from VCS module filename"""
            # VCS modules have different naming but similar logic
            parts = filename.split('.ko.')
            if len(parts) >= 2:
                version = parts[1]
                if '-' in version:
                    dash_parts = version.split('-')
                    if len(dash_parts) >= 2:
                        try:
                            subrev = int(dash_parts[1].split('.')[0])
                            return subrev
                        except ValueError:
                            return 0
            return 0

        for i, pattern in enumerate(patterns):
            self.debug_print(f"    Trying VCS pattern {i+1}/{len(patterns)}: {pattern}")
            files = glob.glob(pattern)
            if files:
                self.debug_print(f"      Found {len(files)} matching files")
                # Sort by version
                files.sort(key=extract_vcs_subrev)

                # Filter out blacklisted versions
                if blacklist_subrevs:
                    filtered_files = []
                    for f in files:
                        file_subrev = extract_vcs_subrev(f)
                        if file_subrev not in blacklist_subrevs:
                            filtered_files.append(f)
                        else:
                            self.debug_print(f"        FILTERED OUT: {f} (subrev {file_subrev} is blacklisted)")
                    files = filtered_files
                    self.debug_print(f"      After blacklist filtering: {len(files)} files remaining")

                if files:
                    selected = files[-1]

                    # NEW: Check if symlink points to /dev/null (disabled module)
                    try:
                        real_path = os.path.realpath(selected)
                        if real_path == '/dev/null':
                            self.debug_print(f"      SKIPPED: {selected} -> /dev/null (disabled module)")
                            continue
                        else:
                            self.debug_print(f"      SELECTED VCS module: {selected}")
                            return selected
                    except OSError:
                        # If readlink fails, assume it's a regular file
                        self.debug_print(f"      SELECTED VCS module: {selected}")
                        return selected

        self.debug_print(f"  No suitable VCS module found for {module_name}")
        return None

    def process_vcs_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process VCS modules (gab, llt, vxfen, amf)"""
        target_dir = f"{top_dir}/veritas/vcs"

        for module in self.vcs_modules:
            local_kmod_dir = f"/opt/VRTS{module}/modules"

            if os.path.exists(local_kmod_dir):
                srcmod = self.find_vcs_module(module, kernel_version, local_kmod_dir, blacklist_subrevs)

                if srcmod:
                    self.create_directory_if_needed(target_dir)
                    target_file = f"{target_dir}/{module}.ko"

                    if self.force or not os.path.exists(target_file):
                        self.myecho(f"/bin/cp -aLf {srcmod} {target_file}")
                        self.myecho(f"/bin/chmod 0755 {srcmod} {target_file}")
                        self.action += 1

    def process_vcsmm_modules(self, kernel_version: str, top_dir: str, blacklist_subrevs: set):
        """Process VCSmm modules"""
        target_dir = f"{top_dir}/veritas/vcs"
        local_kmod_dir = "/opt/VRTSvcs/rac/modules"

        if os.path.exists(local_kmod_dir):
            for module in self.vcsmm_modules:
                srcmod = self.find_vcs_module(module, kernel_version, local_kmod_dir, blacklist_subrevs)

                if srcmod:
                    self.create_directory_if_needed(target_dir)
                    target_file = f"{target_dir}/{module}.ko"

                    if self.force or not os.path.exists(target_file):
                        self.myecho(f"/bin/cp -aLf {srcmod} {target_file}")
                        self.myecho(f"/bin/chmod 0755 {srcmod} {target_file}")
                        self.action += 1

    def setup_selinux_contexts(self):
        """Set up SELinux contexts for VxFS module directories"""
        for vx_mod_dir in self.selinux_dirs:
            if os.path.exists(vx_mod_dir):
                try:
                    # Check if context already exists
                    result = subprocess.run(
                        ['semanage', 'fcontext', '-C', '-l'],
                        capture_output=True, text=True, check=True
                    )

                    context_exists = False
                    for line in result.stdout.split('\n'):
                        if line.startswith(f"{vx_mod_dir} = /lib/modules"):
                            context_exists = True
                            break

                    if not context_exists:
                        self.myecho(f"/usr/sbin/semanage fcontext -a -e /lib/modules {vx_mod_dir}")
                        self.myecho(f"/sbin/restorecon -rFv {vx_mod_dir}")

                except subprocess.CalledProcessError:
                    # semanage might not be available, continue silently
                    pass

    def load_modules(self):
        """Load kernel modules automatically"""
        if self.action > 0:
            for module in self.auto_load_modules:
                self.myecho(f"/usr/sbin/modprobe {module}")

    def run(self):
        """Main execution function"""
        # Check if running on RHEL first
        self.check_rhel_system()

        # Check for vxiod
        if not os.path.exists('/sbin/vxiod') or not os.access('/sbin/vxiod', os.X_OK):
            print("/sbin/vxiod missing!")
            sys.exit(0)

        # Check root privileges
        self.check_root_privileges()

        # Print banner after privilege escalation (unless silent)
        if not self.silent:
            print("#" * 87)
            print(f"###@@### Syntax: {sys.argv[0]} [--force|--silent|--exec|--debug]")
            print("#" * 87)
            print()

        # Get RHEL version
        self.rhel_version = self.get_rhel_version()

        # Get installed kernels
        kernels = self.get_installed_kernels()
        if not kernels:
            print("No kernels found!")
            sys.exit(1)

        # Process each kernel
        total_actions = 0
        self.debug_print(f"Processing {len(kernels)} kernels")
        for kernel_version in kernels:
            self.debug_print(f"=== Processing kernel: {kernel_version} ===")
            self.action = 0
            top_dir = f"/lib/modules/{kernel_version}"

            if not os.path.exists(top_dir):
                self.debug_print(f"Skipping {kernel_version}: {top_dir} does not exist")
                continue

            self.debug_print(f"Working directory: {top_dir}")
            os.chdir(top_dir)

            # Create base veritas directory
            veritas_dir = f"{top_dir}/veritas"
            self.create_directory_if_needed(veritas_dir)

            # Get blacklist subrevisions for this kernel
            blacklist_subrevs = self.get_blacklist_subrevs(kernel_version)

            # Process different module types
            self.debug_print("Processing generic modules...")
            self.process_generic_modules(kernel_version, top_dir, blacklist_subrevs)
            self.debug_print("Processing VxFS modules...")
            self.process_vxfs_modules(kernel_version, top_dir, blacklist_subrevs)
            self.debug_print("Processing VxVM allowed modules...")
            self.process_vxvm_modules(kernel_version, top_dir, blacklist_subrevs)
            self.debug_print("Processing VxVM blacklisted modules...")
            self.process_vxvm_blacklisted_modules(kernel_version, top_dir, blacklist_subrevs)
            self.debug_print("Processing VCS modules...")
            self.process_vcs_modules(kernel_version, top_dir, blacklist_subrevs)
            self.debug_print("Processing VCSmm modules...")
            self.process_vcsmm_modules(kernel_version, top_dir, blacklist_subrevs)

            # Run depmod if we made changes
            if self.action > 0:
                self.debug_print(f"Made {self.action} changes for kernel {kernel_version}, running depmod")
                self.myecho(f"/sbin/depmod -a {kernel_version}")
                total_actions += self.action
            else:
                self.debug_print(f"No changes needed for kernel {kernel_version}")

        # Final operations if any changes were made
        if total_actions > 0:
            self.debug_print(f"Total actions across all kernels: {total_actions}")
            self.debug_print("Running final system operations...")
            self.myecho("/usr/bin/dracut --regenerate-all -o zfs -a lvm -a dm")
            self.myecho("/usr/bin/sync -f /lib/modules")
            self.myecho("/usr/bin/sync -f /boot")
        else:
            self.debug_print("No changes made, skipping final operations")

        # Set up SELinux contexts
        self.debug_print("Setting up SELinux contexts...")
        self.setup_selinux_contexts()

        # Load modules
        self.debug_print("Loading kernel modules...")
        self.load_modules()

        self.debug_print("Script completed successfully")
        sys.exit(0)

def show_version_info(debug_mode=False):
    """Display version information including detected RHEL version"""
    print(__version__)

    # Create a temporary linker instance to detect RHEL version
    linker = VRTSLinker()
    linker.silent = True  # Don't show warnings during version detection
    linker.debug = debug_mode  # Allow debug output if requested

    try:
        rhel_version = linker.get_rhel_version()
        print(f"Detected RHEL version: {rhel_version}")
    except Exception as e:
        print(f"RHEL version detection failed: {e}")

    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(
        description="RHEL Veritas Storage Foundation kernel module relinker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show what would be done
  %(prog)s --force           # Force recreation of existing links
  %(prog)s --silent          # Run quietly
  %(prog)s --exec            # Actually execute the commands
        """
    )

    parser.add_argument('--force', action='store_true',
                       help='Force recreation of existing module links')
    parser.add_argument('--silent', action='store_true',
                       help='Run in silent mode')
    parser.add_argument('--exec', action='store_true',
                       help='Execute commands instead of just displaying them')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing matching logic (prefixed with #)')
    parser.add_argument('--version', action='store_true',
                       help='Show version information and exit')

    args = parser.parse_args()

    # Handle --version flag before any other processing
    if args.version:
        show_version_info(debug_mode=args.debug)

    # Create and configure the linker
    linker = VRTSLinker()
    linker.force = args.force
    linker.silent = args.silent
    linker.run_exec = args.exec
    linker.debug = args.debug

    # If --exec is specified, also set silent mode
    if args.exec:
        linker.silent = True

    # Run the main functionality (banner will be printed after privilege check)
    linker.run()

if __name__ == "__main__":
    main()
