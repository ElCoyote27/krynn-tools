#!/usr/bin/env python3
# $Id: RHEL_VRTS_links.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of RHEL_VRTS_links bash script
# Maintains same command syntax: --force, --silent, --exec

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
        self.action = 0
        self.rhel_version = None

        # Module groups and their target directories
        self.generic_modules = ['veki', 'vxglm', 'vxgms', 'vxodm', 'storageapi']
        self.vxfs_modules = ['vxfs', 'fdd', 'vxportal', 'vxcafs']
        self.vxvm_modules = [
            'vxdmp', 'vxio', 'vxspec',
            'dmpaaa', 'dmpaa', 'dmpalua', 'dmpapf', 'dmpapg', 'dmpap',
            'dmpCLARiiON', 'dmpEngenio', 'dmphuawei', 'dmpvmax',
            'dmpinv', 'dmpjbod', 'dmpnalsi', 'dmpnvme', 'dmpsun7x10alua', 'dmpsvc'
            # Note: dmpkove excluded due to symbol issues
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

    def myecho(self, command: str):
        """Execute command or print it based on run_exec flag"""
        if not self.run_exec:
            print(command)
        else:
            try:
                result = subprocess.run(command, shell=True, check=True,
                                      capture_output=False)
                return result.returncode == 0
            except subprocess.CalledProcessError as e:
                print(f"Command failed: {command}")
                return False
            except Exception as e:
                print(f"Error executing command: {command} - {e}")
                return False

    def check_root_privileges(self):
        """Check if running as root, exec sudo if not"""
        if os.getuid() != 0:
            # Re-exec with sudo
            sudo_cmd = ['sudo'] + sys.argv
            os.execvp('sudo', sudo_cmd)

    def get_rhel_version(self):
        """Get RHEL version using lsb_release"""
        try:
            result = subprocess.run(['lsb_release', '-r'],
                                  capture_output=True, text=True, check=True)
            # Extract version number (e.g., "Release: 8.5" -> "8")
            version_line = result.stdout.strip()
            version = version_line.split(':')[1].strip().split('.')[0]
            return int(version)
        except (subprocess.CalledProcessError, ValueError, IndexError):
            print("Warning: Could not determine RHEL version")
            return 8  # Default to 8

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
            return kernels
        except subprocess.CalledProcessError:
            print("Error: Could not query installed kernels")
            return []

    def get_blacklist_subrevs(self, kernel_version: str) -> set:
        """Get set of subrevision numbers to blacklist for kernel modules

        This replicates the bash logic but returns a clean set instead of a regex pattern
        """
        try:
            # Get all available subrevisions from /etc/vx/kernel
            kmod_dir = Path('/etc/vx/kernel')
            if not kmod_dir.exists():
                return set()

            ko_files = list(kmod_dir.glob('*.ko.*'))
            all_subrevs = set()

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
                        except ValueError:
                            return set()
                    else:
                        return set()
                else:
                    return set()
            else:
                return set()

            # Return subrevisions greater than current (these should be blacklisted)
            return {subrev for subrev in all_subrevs if subrev > my_subrev}

        except Exception:
            return set()

    def find_best_module(self, module_name: str, kernel_version: str,
                        kmod_dir: str, blacklist_subrevs: set) -> Optional[str]:
        """Find the best matching kernel module for given kernel version

        This replicates the bash logic but uses a clean set-based blacklist approach
        """
        krev = kernel_version.split('-')[0]  # e.g., "5.14.0"
        ksubrev = '.'.join(kernel_version.split('.')[:3])  # e.g., "5.14.0"

        # Try with full subrevision first (KSUBREV), then with base revision (KREV)
        patterns = [
            f"{kmod_dir}/{module_name}.ko.{ksubrev}.*",
            f"{kmod_dir}/{module_name}.ko.{krev}-*"
        ]

        for pattern in patterns:
            files = glob.glob(pattern)
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

                # Filter out blacklisted versions - much cleaner than regex matching
                if blacklist_subrevs:
                    filtered_files = []
                    for f in files:
                        file_subrev = version_key(f)  # Extract subrev from filename
                        if file_subrev not in blacklist_subrevs:
                            filtered_files.append(f)
                    files = filtered_files

                if files:
                    return files[-1]  # Return the latest version (tail -1 equivalent)

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
        """Process VxVM modules"""
        kmod_dir = '/etc/vx/kernel'
        target_dir = f"{top_dir}/veritas/vxvm"

        self.create_directory_if_needed(target_dir)

        for module in self.vxvm_modules:
            srcmod = self.find_best_module(module, kernel_version, kmod_dir, blacklist_subrevs)
            if srcmod:
                target_file = f"{target_dir}/{module}.ko"

                if self.force or not os.path.exists(target_file) or os.path.getsize(target_file) == 0:
                    self.myecho(f"/bin/ln -sf {srcmod} {target_file}")
                    self.action += 1

    def find_vcs_module(self, module_name: str, kernel_version: str,
                       local_kmod_dir: str, blacklist_subrevs: set) -> Optional[str]:
        """Find VCS module with complex pattern matching"""
        krev = kernel_version.split('-')[0]
        ksubrev = '.'.join(kernel_version.split('.')[:3])

        patterns = [
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*el[7-9]_[0-9]*.x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*el[7-9]_[0-9]*.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*el[7-9].x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{ksubrev}*el[7-9].x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*el[7-9]_[0-9]*.x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*el[7-9]_[0-9]*.x86_64",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*el[7-9].x86_64-nonrdma",
            f"{local_kmod_dir}/{module_name}.ko.{krev}*el[7-9].x86_64"
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

        for pattern in patterns:
            files = glob.glob(pattern)
            if files:
                # Sort by version
                files.sort(key=extract_vcs_subrev)

                # Filter out blacklisted versions - clean approach
                if blacklist_subrevs:
                    filtered_files = []
                    for f in files:
                        file_subrev = extract_vcs_subrev(f)
                        if file_subrev not in blacklist_subrevs:
                            filtered_files.append(f)
                    files = filtered_files

                if files:
                    return files[-1]

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
                        self.myecho(f"semanage fcontext -a -e /lib/modules {vx_mod_dir}")
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
        # Check for vxiod
        if not os.path.exists('/sbin/vxiod') or not os.access('/sbin/vxiod', os.X_OK):
            print("/sbin/vxiod missing!")
            sys.exit(0)

        # Check root privileges
        self.check_root_privileges()

        # Get RHEL version
        self.rhel_version = self.get_rhel_version()

        # Get installed kernels
        kernels = self.get_installed_kernels()
        if not kernels:
            print("No kernels found!")
            sys.exit(1)

        # Process each kernel
        total_actions = 0
        for kernel_version in kernels:
            self.action = 0
            top_dir = f"/lib/modules/{kernel_version}"

            if not os.path.exists(top_dir):
                continue

            os.chdir(top_dir)

            # Create base veritas directory
            veritas_dir = f"{top_dir}/veritas"
            self.create_directory_if_needed(veritas_dir)

            # Get blacklist subrevisions for this kernel
            blacklist_subrevs = self.get_blacklist_subrevs(kernel_version)

            # Process different module types
            self.process_generic_modules(kernel_version, top_dir, blacklist_subrevs)
            self.process_vxfs_modules(kernel_version, top_dir, blacklist_subrevs)
            self.process_vxvm_modules(kernel_version, top_dir, blacklist_subrevs)
            self.process_vcs_modules(kernel_version, top_dir, blacklist_subrevs)
            self.process_vcsmm_modules(kernel_version, top_dir, blacklist_subrevs)

            # Run depmod if we made changes
            if self.action > 0:
                self.myecho(f"/sbin/depmod -a {kernel_version}")
                total_actions += self.action

        # Final operations if any changes were made
        if total_actions > 0:
            self.myecho("/usr/bin/dracut --regenerate-all -o zfs -a lvm -a dm")
            self.myecho("/usr/bin/sync -f /lib/modules")
            self.myecho("/usr/bin/sync -f /boot")

        # Set up SELinux contexts
        self.setup_selinux_contexts()

        # Load modules
        self.load_modules()

        sys.exit(0)

def main():
    parser = argparse.ArgumentParser(
        description="RHEL Veritas Storage Foundation kernel module linker",
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

    args = parser.parse_args()

    # Create and configure the linker
    linker = VRTSLinker()
    linker.force = args.force
    linker.silent = args.silent
    linker.run_exec = args.exec

    # If --exec is specified, also set silent mode
    if args.exec:
        linker.silent = True

    # Print banner unless silent
    if not linker.silent:
        print("#" * 87)
        print(f"###@@### Syntax: {sys.argv[0]} [--force|--silent|--exec]")
        print("#" * 87)
        print()

    # Run the main functionality
    linker.run()

if __name__ == "__main__":
    main()
