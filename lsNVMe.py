#!/usr/bin/env python3
#
# $Id: lsNVMe.py,v 1.03 2025/09/06 16:00:00 add-lba-size Exp $
#
# NVMe Device Health and Temperature Monitor
# Shows NVMe devices with temperature, health status, and SMART attributes
# Uses nvme smart-log and smartctl commands to extract comprehensive health data

__version__ = "lsNVMe.py 1.03 2025/09/06 16:00:00 add-lba-size Exp"

#
# VERSION HISTORY:
# ================
#
# v1.03 (2025-09-06): Added LBA size display
#   - Added LBA size column to show logical block address size (512B, 4K, etc.)
#   - Extracts LBA format information from nvme id-ns and smartctl outputs
#   - Helps identify drives using modern 4K sectors vs legacy 512-byte sectors
#
# v1.02 (2025-09-06): Added firmware version display
#   - Added firmware version column to device information table
#   - Extracts firmware version from smartctl output and nvme id-ctrl when available
#   - Enhanced device identification with both model and firmware version
#
# v1.01 (2025-09-06): Python 3.6+ compatibility
#   - Ensured full compatibility with Python 3.6+ (tested on RHEL8 Python 3.6)
#   - Uses subprocess.run() with stdout/stderr=subprocess.PIPE for older Python versions
#   - Uses universal_newlines=True instead of text=True for Python 3.6 compatibility
#   - Verified compatibility with typing module and f-strings (Python 3.6+)
#
# v1.00 (2025-09-06): Initial release
#   - NVMe device discovery and health monitoring
#   - Automatic privilege escalation when needed
#   - Extracts temperature, wear level, error counts, and health status
#   - Concise tabular output format with one line per device
#   - Support for both nvme smart-log and smartctl data sources
#   - Debug mode and version information
#   - Terminal width detection and dynamic column sizing
#   - Handles both NVMe and SATA SSDs through smartctl fallback
#

import os
import sys
import re
import subprocess
import argparse
import shutil
import glob
from typing import List, Dict, Tuple, Optional

class NVMeHealthAnalyzer:
    def __init__(self):
        self.debug = False
        self.nvme_path = None
        self.smartctl_path = None

        # Find required tools
        self.find_tools()

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def find_tools(self):
        """Locate nvme and smartctl tools"""
        # Find nvme tool
        common_nvme_paths = ['/usr/sbin/nvme', '/usr/bin/nvme', '/sbin/nvme']
        
        for path in common_nvme_paths:
            if os.path.exists(path) and os.access(path, os.X_OK):
                self.nvme_path = path
                break
        
        if not self.nvme_path:
            nvme_path = shutil.which('nvme')
            if nvme_path:
                self.nvme_path = nvme_path

        # Find smartctl tool
        common_smartctl_paths = ['/usr/sbin/smartctl', '/usr/bin/smartctl', '/sbin/smartctl']
        
        for path in common_smartctl_paths:
            if os.path.exists(path) and os.access(path, os.X_OK):
                self.smartctl_path = path
                break
        
        if not self.smartctl_path:
            smartctl_path = shutil.which('smartctl')
            if smartctl_path:
                self.smartctl_path = smartctl_path

        if not self.nvme_path and not self.smartctl_path:
            print("Unable to find 'nvme' or 'smartctl' commands! At least one is required. Aborting...")
            sys.exit(126)

    def is_root(self) -> bool:
        """Check if running as root"""
        return os.geteuid() == 0

    def has_sudo_token(self) -> bool:
        """Check if user has a valid sudo token (can sudo without password prompt)"""
        try:
            # Try a simple sudo command with short timeout - if it works without prompting, we have a token
            result = subprocess.run(['sudo', '-n', 'true'], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  timeout=2)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            return False

    def discover_nvme_devices(self) -> List[str]:
        """Discover NVMe devices in the system"""
        devices = []
        
        # Look for NVMe devices in /dev
        nvme_pattern = '/dev/nvme[0-9]*n[0-9]*'
        potential_devices = glob.glob(nvme_pattern)
        
        for device in potential_devices:
            # Only include namespace devices (nvme0n1, nvme1n1, etc.), not controllers
            if re.match(r'/dev/nvme\d+n\d+$', device):
                # Check if device exists (but don't check read access here - we'll handle permissions later)
                if os.path.exists(device):
                    devices.append(device)
                    self.debug_print(f"Found NVMe device: {device}")
                elif not os.path.exists(device):
                    self.debug_print(f"Device {device} does not exist, skipping")
        
        # Sort devices naturally
        devices.sort(key=lambda x: [int(n) if n.isdigit() else n for n in re.split(r'(\d+)', x)])
        
        self.debug_print(f"Discovered {len(devices)} total NVMe devices: {devices}")
        
        if not devices:
            self.debug_print("No NVMe devices found. Checking /dev/ directory...")
            # Debug: Show what's actually in /dev that looks NVMe-related
            try:
                all_nvme = glob.glob('/dev/nvme*')
                if all_nvme:
                    self.debug_print(f"Found these /dev/nvme* entries: {all_nvme}")
                else:
                    self.debug_print("No /dev/nvme* entries found at all")
            except Exception as e:
                self.debug_print(f"Error checking /dev directory: {e}")
        
        return devices

    def run_nvme_smart_log(self, device: str) -> Optional[str]:
        """Run nvme smart-log on a device"""
        if not self.nvme_path:
            return None
            
        self.debug_print(f"Running nvme smart-log on {device}")
        
        try:
            # Try without sudo first
            result = subprocess.run([self.nvme_path, 'smart-log', device],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  universal_newlines=True, timeout=30)
            
            if result.returncode == 0:
                self.debug_print(f"Got nvme smart-log data for {device}")
                return result.stdout
            elif not self.is_root():
                # Try with sudo
                self.debug_print(f"Trying nvme smart-log with sudo for {device}")
                result = subprocess.run(['sudo', self.nvme_path, 'smart-log', device],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=60)
                if result.returncode == 0:
                    self.debug_print(f"Got nvme smart-log data with sudo for {device}")
                    return result.stdout
        
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        self.debug_print(f"Failed to get nvme smart-log data for {device}")
        return None

    def run_nvme_id_ctrl(self, device: str) -> Optional[str]:
        """Run nvme id-ctrl on a device to get controller information including firmware"""
        if not self.nvme_path:
            return None
            
        self.debug_print(f"Running nvme id-ctrl on {device}")
        
        try:
            # Try without sudo first
            result = subprocess.run([self.nvme_path, 'id-ctrl', device],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  universal_newlines=True, timeout=30)
            
            if result.returncode == 0:
                self.debug_print(f"Got nvme id-ctrl data for {device}")
                return result.stdout
            elif not self.is_root():
                # Try with sudo
                self.debug_print(f"Trying nvme id-ctrl with sudo for {device}")
                result = subprocess.run(['sudo', self.nvme_path, 'id-ctrl', device],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=60)
                if result.returncode == 0:
                    self.debug_print(f"Got nvme id-ctrl data with sudo for {device}")
                    return result.stdout
        
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        self.debug_print(f"Failed to get nvme id-ctrl data for {device}")
        return None

    def run_nvme_id_ns(self, device: str) -> Optional[str]:
        """Run nvme id-ns -H on a device to get namespace information including LBA size"""
        if not self.nvme_path:
            return None
            
        self.debug_print(f"Running nvme id-ns -H on {device}")
        
        try:
            # Try without sudo first (with -H for human readable format)
            result = subprocess.run([self.nvme_path, 'id-ns', '-H', device],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  universal_newlines=True, timeout=30)
            
            if result.returncode == 0:
                self.debug_print(f"Got nvme id-ns -H data for {device}")
                return result.stdout
            elif not self.is_root():
                # Try with sudo
                self.debug_print(f"Trying nvme id-ns -H with sudo for {device}")
                result = subprocess.run(['sudo', self.nvme_path, 'id-ns', '-H', device],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=60)
                if result.returncode == 0:
                    self.debug_print(f"Got nvme id-ns -H data with sudo for {device}")
                    return result.stdout
        
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        self.debug_print(f"Failed to get nvme id-ns -H data for {device}")
        return None

    def run_smartctl(self, device: str) -> Optional[str]:
        """Run smartctl on a device"""
        if not self.smartctl_path:
            return None
            
        self.debug_print(f"Running smartctl on {device}")
        
        try:
            # Try without sudo first
            result = subprocess.run([self.smartctl_path, '-a', device],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, timeout=30)
            
            if result.returncode == 0 or result.returncode == 4:  # 4 is warning, still usable
                self.debug_print(f"Got smartctl data for {device}")
                return result.stdout
            elif not self.is_root():
                # Try with sudo
                self.debug_print(f"Trying smartctl with sudo for {device}")
                result = subprocess.run(['sudo', self.smartctl_path, '-a', device],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=60)
                if result.returncode == 0 or result.returncode == 4:
                    self.debug_print(f"Got smartctl data with sudo for {device}")
                    return result.stdout
        
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        self.debug_print(f"Failed to get smartctl data for {device}")
        return None

    def parse_nvme_smart_log(self, smart_output: str) -> Dict[str, str]:
        """Parse nvme smart-log output"""
        data = {}
        
        if not smart_output:
            return data
        
        self.debug_print("Parsing nvme smart-log output for wear information")
        
        # Parse temperature
        temp_match = re.search(r'temperature\s*:\s*(\d+)\s*Celsius', smart_output, re.IGNORECASE)
        if temp_match:
            data['temperature'] = f"{temp_match.group(1)}°C"
        
        # Parse percentage used (wear level) - try multiple formats
        wear_patterns = [
            r'percentage_used\s*:\s*(\d+)%',  # percentage_used : X%
            r'percentage\s+used\s*:\s*(\d+)%',  # percentage used : X%
            r'percentage_used\s*:\s*(\d+)\s*%',  # percentage_used : X %
            r'percentage\s+used\s*:\s*(\d+)\s*%',  # percentage used : X %
        ]
        
        wear_found = False
        for pattern in wear_patterns:
            wear_match = re.search(pattern, smart_output, re.IGNORECASE)
            if wear_match:
                wear_value = wear_match.group(1)
                data['wear_level'] = f"{wear_value}%"
                self.debug_print(f"Found wear level {wear_value}% using pattern: {pattern}")
                wear_found = True
                break
        
        if not wear_found:
            self.debug_print("No wear level found in nvme smart-log output")
        
        # Parse data units written
        written_match = re.search(r'data_units_written\s*:\s*([\d,]+)', smart_output, re.IGNORECASE)
        if written_match:
            data['data_written'] = written_match.group(1)
        
        # Parse critical warnings
        warning_match = re.search(r'critical_warning\s*:\s*0x([0-9a-fA-F]+)', smart_output, re.IGNORECASE)
        if warning_match:
            warning_val = int(warning_match.group(1), 16)
            data['critical_warning'] = f"0x{warning_val:02x}"
        
        # Parse power on hours
        power_match = re.search(r'power_on_hours\s*:\s*([\d,]+)', smart_output, re.IGNORECASE)
        if power_match:
            data['power_hours'] = power_match.group(1)
        
        return data

    def parse_smartctl_output(self, smartctl_output: str) -> Dict[str, str]:
        """Parse smartctl output"""
        data = {}
        
        if not smartctl_output:
            return data
        
        self.debug_print("Parsing smartctl output for sector size information")
        
        # Parse overall health
        health_match = re.search(r'SMART overall-health self-assessment test result:\s*(.+)', smartctl_output, re.IGNORECASE)
        if health_match:
            data['health'] = health_match.group(1).strip()
        
        # Parse firmware version
        firmware_patterns = [
            r'Firmware Version:\s*(.+)',
            r'Revision:\s*(.+)'
        ]
        
        for pattern in firmware_patterns:
            firmware_match = re.search(pattern, smartctl_output, re.IGNORECASE)
            if firmware_match:
                data['firmware'] = firmware_match.group(1).strip()
                break
        
        # Parse temperature (multiple possible formats)
        temp_patterns = [
            r'Temperature:\s*(\d+)\s*Celsius',
            r'Current Drive Temperature:\s*(\d+)\s*C',
            r'Temperature_Celsius\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*(\d+)'
        ]
        
        for pattern in temp_patterns:
            temp_match = re.search(pattern, smartctl_output, re.IGNORECASE)
            if temp_match:
                data['temperature'] = f"{temp_match.group(1)}°C"
                break
        
        # Parse wear leveling
        wear_patterns = [
            r'Percentage Used:\s*(\d+)%',
            r'Wear_Leveling_Count\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*(\d+)',
            r'Media Wearout Indicator:\s*(\d+)',
            r'Wear Leveling Count:\s*(\d+)',
            r'Available Spare:\s*(\d+)%'
        ]
        
        wear_found = False
        for pattern in wear_patterns:
            wear_match = re.search(pattern, smartctl_output, re.IGNORECASE)
            if wear_match:
                wear_value = wear_match.group(1)
                data['wear_level'] = f"{wear_value}%"
                self.debug_print(f"Found wear level {wear_value}% using smartctl pattern: {pattern}")
                wear_found = True
                break
        
        if not wear_found:
            self.debug_print("No wear level found in smartctl output")
        
        # Parse power on hours
        power_patterns = [
            r'Power On Hours:\s*([\d,]+)',
            r'Power_On_Hours\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*([\d,]+)'
        ]
        
        for pattern in power_patterns:
            power_match = re.search(pattern, smartctl_output, re.IGNORECASE)
            if power_match:
                data['power_hours'] = power_match.group(1)
                break
        
        # Parse error count
        error_patterns = [
            r'Media and Data Integrity Errors:\s*(\d+)',
            r'Reallocated_Sector_Ct\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*\S+\s*(\d+)'
        ]
        
        for pattern in error_patterns:
            error_match = re.search(pattern, smartctl_output, re.IGNORECASE)
            if error_match:
                data['error_count'] = error_match.group(1)
                break
        
        # Parse sector size / LBA size - current active size
        current_sector = 'N/A'
        supports_4k = 'N/A'
        supported_sizes = set()
        
        # First try to parse the "Supported LBA Sizes" table from smartctl
        # Look for lines like: " 1 +    4096       0         0"
        lba_table_section = False
        for line in smartctl_output.split('\n'):
            if 'Supported LBA Sizes' in line:
                lba_table_section = True
                self.debug_print("Found 'Supported LBA Sizes' section in smartctl output")
                continue
            elif lba_table_section:
                # Parse lines like " 1 +    4096       0         0" or " 0 -     512       0         2"
                lba_table_match = re.match(r'\s*(\d+)\s+([+-])\s+(\d+)', line.strip())
                if lba_table_match:
                    format_id, current_marker, data_size = lba_table_match.groups()
                    lba_size = int(data_size)
                    self.debug_print(f"LBA format {format_id}: {lba_size} bytes, current={current_marker == '+'}")
                    
                    # Add to supported sizes
                    if lba_size == 512:
                        supported_sizes.add('512B')
                    elif lba_size == 4096:
                        supported_sizes.add('4K')
                    elif lba_size == 520:
                        supported_sizes.add('520B')
                    else:
                        supported_sizes.add(f'{lba_size}B')
                    
                    # Check if this is the current format (marked with +)
                    if current_marker == '+':
                        if lba_size == 512:
                            current_sector = '512B'
                        elif lba_size == 4096:
                            current_sector = '4K'
                        elif lba_size == 520:
                            current_sector = '520B'
                        else:
                            current_sector = f'{lba_size}B'
                        self.debug_print(f"Current format {format_id} uses {lba_size} bytes per sector")
                elif line.strip() == '':
                    # Empty line might end the LBA table section
                    continue
                else:
                    # If we encounter a non-matching line, we might be past the table
                    if line.strip() and not re.match(r'\s*Id\s+Fmt\s+Data', line):
                        lba_table_section = False
        
        # If we didn't find the LBA table, try standard patterns
        if current_sector == 'N/A':
            self.debug_print("LBA sizes table not found, trying standard sector size patterns")
            lba_patterns = [
                r'Sector Size:\s*(\d+)\s*bytes',
                r'Sector Sizes:\s*(\d+)\s*bytes logical',
                r'(\d+)\s*bytes logical',
                r'Logical block size:\s*(\d+)\s*bytes',
                r'Logical/Physical:\s*(\d+)\s*bytes',
                r'Logical Sector Size:\s*(\d+)\s*bytes',
                r'Physical Sector Size:\s*(\d+)\s*bytes',
                r'User Capacity:.*\[(\d+)\s*bytes per sector\]'
            ]
            
            for pattern in lba_patterns:
                lba_match = re.search(pattern, smartctl_output, re.IGNORECASE)
                if lba_match:
                    lba_size = int(lba_match.group(1))
                    self.debug_print(f"Found sector size {lba_size} bytes using pattern: {pattern}")
                    if lba_size == 512:
                        current_sector = '512B'
                        supported_sizes.add('512B')
                    elif lba_size == 4096:
                        current_sector = '4K'
                        supported_sizes.add('4K')
                    elif lba_size == 520:
                        current_sector = '520B'
                        supported_sizes.add('520B')
                    else:
                        current_sector = f'{lba_size}B'
                        supported_sizes.add(f'{lba_size}B')
                    break
        
        if current_sector == 'N/A':
            self.debug_print("No sector size found in smartctl output with any patterns")
        
        # Determine 4K support based on what we found
        if '4K' in supported_sizes:
            supports_4k = 'Yes'
        elif len(supported_sizes) > 0 and '4K' not in supported_sizes:
            supports_4k = 'No'
        elif current_sector == '4K':
            supports_4k = 'Yes'  # If currently using 4K, it obviously supports it
        else:
            # Fallback: Look for indications of 4K support anywhere in the output
            if re.search(r'4096.*bytes', smartctl_output, re.IGNORECASE):
                supports_4k = 'Yes'
            elif current_sector in ['512B', '520B']:
                supports_4k = 'No'  # If only legacy sizes found, likely no 4K support
        
        data['supports_4k'] = supports_4k
        data['current_sector'] = current_sector
        
        return data

    def parse_nvme_id_ctrl(self, id_ctrl_output: str) -> Dict[str, str]:
        """Parse nvme id-ctrl output"""
        data = {}
        
        if not id_ctrl_output:
            return data
        
        # Parse firmware revision
        firmware_match = re.search(r'fr\s*:\s*(.+)', id_ctrl_output, re.IGNORECASE)
        if firmware_match:
            data['firmware'] = firmware_match.group(1).strip()
        
        # Parse model number
        model_match = re.search(r'mn\s*:\s*(.+)', id_ctrl_output, re.IGNORECASE)
        if model_match:
            data['model'] = model_match.group(1).strip()
        
        return data

    def parse_nvme_id_ns(self, id_ns_output: str) -> Dict[str, str]:
        """Parse nvme id-ns output"""
        data = {}
        
        if not id_ns_output:
            self.debug_print("No nvme id-ns output to parse")
            return data
        
        self.debug_print("Parsing nvme id-ns output for LBA format information")
        
        # Parse LBA formats to find what's supported and what's currently active
        # NVMe id-ns -H output shows formats like:
        # "LBA Format  0 : Metadata Size: 0   bytes - Data Size: 512 bytes - Relative Performance: 0x2 Good"
        # "LBA Format  1 : Metadata Size: 0   bytes - Data Size: 4096 bytes - Relative Performance: 0 Best (in use)"
        
        supported_sizes = set()
        current_sector_size = 'N/A'
        
        # Find all supported LBA formats using the -H (human readable) format
        lba_format_matches = re.findall(r'LBA Format\s+(\d+)\s*:.*Data Size:\s*(\d+)\s*bytes(.*)$', id_ns_output, re.MULTILINE | re.IGNORECASE)
        if lba_format_matches:
            self.debug_print(f"Found {len(lba_format_matches)} LBA format entries (human readable format)")
            for format_num, data_size, extra_info in lba_format_matches:
                lba_size = int(data_size)
                self.debug_print(f"LBA format {format_num}: {lba_size} bytes - {extra_info.strip()}")
                
                # Add to supported sizes
                if lba_size == 512:
                    supported_sizes.add('512B')
                elif lba_size == 4096:
                    supported_sizes.add('4K')
                elif lba_size == 520:
                    supported_sizes.add('520B')  # Some enterprise drives use 520
                else:
                    supported_sizes.add(f'{lba_size}B')
                
                # Check if this is the current format (marked with "in use")
                if '(in use)' in extra_info.lower():
                    if lba_size == 512:
                        current_sector_size = '512B'
                    elif lba_size == 4096:
                        current_sector_size = '4K'
                    elif lba_size == 520:
                        current_sector_size = '520B'
                    else:
                        current_sector_size = f'{lba_size}B'
                    self.debug_print(f"Current format {format_num} uses {lba_size} bytes per sector")
        
        else:
            # Fallback to old format parsing (raw nvme id-ns without -H)
            self.debug_print("Human readable format not found, trying raw format")
            lbaf_matches = re.findall(r'lbaf\s*(\d+)\s*:.*lbads:(\d+)', id_ns_output, re.IGNORECASE)
            if lbaf_matches:
                self.debug_print(f"Found {len(lbaf_matches)} LBA format entries (raw format)")
                for format_num, lbads in lbaf_matches:
                    lba_size = 2 ** int(lbads)
                    self.debug_print(f"LBA format {format_num}: {lba_size} bytes (lbads={lbads})")
                    if lba_size == 512:
                        supported_sizes.add('512B')
                    elif lba_size == 4096:
                        supported_sizes.add('4K')
                    elif lba_size == 520:
                        supported_sizes.add('520B')
                    else:
                        supported_sizes.add(f'{lba_size}B')
                
                # Look for the currently active LBA format
                current_lba_match = re.search(r'in use.*:\s*(\d+)', id_ns_output, re.IGNORECASE)
                if current_lba_match:
                    current_format = int(current_lba_match.group(1))
                    self.debug_print(f"Current LBA format in use: {current_format}")
                    
                    # Find the corresponding lbaf entry with the LBA data size
                    lba_format_pattern = rf'lbaf\s*{current_format}\s*:.*lbads:(\d+)'
                    lbads_match = re.search(lba_format_pattern, id_ns_output, re.IGNORECASE)
                    if lbads_match:
                        lbads = int(lbads_match.group(1))
                        lba_size = 2 ** lbads  # LBA data size is 2^lbads bytes
                        self.debug_print(f"Current format {current_format} uses {lba_size} bytes per sector")
                        
                        if lba_size == 512:
                            current_sector_size = '512B'
                        elif lba_size == 4096:
                            current_sector_size = '4K'
                        elif lba_size == 520:
                            current_sector_size = '520B'
                        else:
                            current_sector_size = f'{lba_size}B'
                    else:
                        self.debug_print(f"Could not find lbads for current format {current_format}")
                else:
                    self.debug_print("Could not find 'in use' format indicator")
            else:
                self.debug_print("No LBA format entries found in raw format either")
        
        # Set data fields
        data['supports_4k'] = 'Yes' if '4K' in supported_sizes else 'No'
        data['current_sector'] = current_sector_size
        
        # Alternative parsing for different output formats if we didn't get the info above
        if current_sector_size == 'N/A':
            # Look for more direct LBA size mentions
            lba_direct_patterns = [
                r'LBA Format.*:\s*(\d+)\s*bytes',
                r'Block Size:\s*(\d+)\s*bytes'
            ]
            
            for pattern in lba_direct_patterns:
                lba_match = re.search(pattern, id_ns_output, re.IGNORECASE)
                if lba_match:
                    lba_size = int(lba_match.group(1))
                    if lba_size == 512:
                        data['current_sector'] = '512B'
                    elif lba_size == 4096:
                        data['current_sector'] = '4K'
                    else:
                        data['current_sector'] = f'{lba_size}B'
                    break
        
        return data

    def get_device_info(self, device: str) -> Dict[str, str]:
        """Get comprehensive device information"""
        info = {
            'device': device,
            'temperature': 'N/A',
            'health': 'N/A',
            'wear_level': 'N/A',
            'power_hours': 'N/A',
            'error_count': 'N/A',
            'firmware': 'N/A',
            'supports_4k': 'N/A',
            'current_sector': 'N/A',
            'model': 'N/A'
        }
        
        # Get nvme smart-log data
        nvme_data = {}
        smart_output = self.run_nvme_smart_log(device)
        if smart_output:
            nvme_data = self.parse_nvme_smart_log(smart_output)
        
        # Get nvme id-ctrl data
        nvme_id_data = {}
        id_ctrl_output = self.run_nvme_id_ctrl(device)
        if id_ctrl_output:
            nvme_id_data = self.parse_nvme_id_ctrl(id_ctrl_output)
        
        # Get nvme id-ns data (namespace information including LBA size)
        nvme_ns_data = {}
        id_ns_output = self.run_nvme_id_ns(device)
        if id_ns_output:
            nvme_ns_data = self.parse_nvme_id_ns(id_ns_output)
        
        # Get smartctl data
        smartctl_data = {}
        smartctl_output = self.run_smartctl(device)
        if smartctl_output:
            smartctl_data = self.parse_smartctl_output(smartctl_output)
            # Extract model from smartctl if not already found
            if 'model' not in nvme_id_data:
                model_match = re.search(r'Device Model:\s*(.+)|Model Number:\s*(.+)', smartctl_output)
                if model_match:
                    info['model'] = (model_match.group(1) or model_match.group(2)).strip()
        
        # Merge data (priority: nvme id-ctrl > nvme smart-log > smartctl)
        for key in ['temperature', 'wear_level', 'power_hours']:
            if key in smartctl_data:
                info[key] = smartctl_data[key]
            if key in nvme_data:  # nvme smart-log data overrides smartctl
                info[key] = nvme_data[key]
        
        # Health status from smartctl
        if 'health' in smartctl_data:
            info['health'] = smartctl_data['health']
        
        # Error count from smartctl
        if 'error_count' in smartctl_data:
            info['error_count'] = smartctl_data['error_count']
        
        # Model and firmware from nvme id-ctrl (highest priority) or smartctl
        if 'model' in nvme_id_data:
            info['model'] = nvme_id_data['model']
        elif 'model' in smartctl_data:
            info['model'] = smartctl_data['model']
        
        if 'firmware' in nvme_id_data:
            info['firmware'] = nvme_id_data['firmware']
        elif 'firmware' in smartctl_data:
            info['firmware'] = smartctl_data['firmware']
        
        # 4K support and current sector size from nvme id-ns (highest priority) or smartctl
        if 'supports_4k' in nvme_ns_data:
            info['supports_4k'] = nvme_ns_data['supports_4k']
        elif 'supports_4k' in smartctl_data:
            info['supports_4k'] = smartctl_data['supports_4k']
        
        if 'current_sector' in nvme_ns_data:
            info['current_sector'] = nvme_ns_data['current_sector']
        elif 'current_sector' in smartctl_data:
            info['current_sector'] = smartctl_data['current_sector']
        
        return info

    def get_terminal_width(self) -> int:
        """Get terminal width, with intelligent defaults for piped output"""
        # Check if stdout is connected to a terminal
        if not sys.stdout.isatty():
            # Output is being piped/redirected, use a generous width
            return 200

        try:
            return shutil.get_terminal_size().columns
        except:
            try:
                result = subprocess.run(['stty', 'size'], stdout=subprocess.PIPE, 
                                      stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    _, cols = result.stdout.strip().split()
                    return int(cols)
            except:
                pass
        return 120  # Default fallback for terminal

    def format_current_sector(self, current_sector: str) -> str:
        """Format current sector size display"""
        return current_sector

    def display_devices(self, devices_info: List[Dict], show_all: bool = False):
        """Display NVMe devices in a formatted table"""
        
        if not devices_info:
            print("No NVMe devices found.")
            return
        
        # Filter devices if not showing all
        if not show_all:
            # Filter out devices with no useful information
            devices_info = [d for d in devices_info if 
                          d['temperature'] != 'N/A' or d['health'] != 'N/A' or 
                          d['wear_level'] != 'N/A' or d['model'] != 'N/A' or 
                          d['firmware'] != 'N/A' or d['supports_4k'] != 'N/A' or 
                          d['current_sector'] != 'N/A']
        
        if not devices_info:
            print("No NVMe devices found with health information.")
            return
        
        # Get terminal width and calculate description width
        terminal_width = self.get_terminal_width()
        
        # Calculate column widths
        max_widths = {
            'device': max(len('Device'), max(len(d['device']) for d in devices_info)),
            'temperature': max(len('Temp'), max(len(str(d['temperature'])) for d in devices_info)),
            'supports_4k': max(len('4K?'), max(len(str(d['supports_4k'])) for d in devices_info)),
            'current_sector': max(len('Current'), max(len(self.format_current_sector(str(d['current_sector']))) for d in devices_info)),
            'health': max(len('Health'), max(len(str(d['health'])) for d in devices_info)),
            'wear_level': max(len('Wear'), max(len(str(d['wear_level'])) for d in devices_info)),
            'power_hours': max(len('PowerHrs'), max(len(str(d['power_hours'])) for d in devices_info)),
            'error_count': max(len('Errors'), max(len(str(d['error_count'])) for d in devices_info)),
            'firmware': max(len('Firmware'), max(len(str(d['firmware'])) for d in devices_info)),
        }
        
        # Calculate remaining space for model
        used_width = sum(max_widths.values()) + 18  # 18 for spacing between columns
        model_width = max(20, terminal_width - used_width - 5)  # Minimum 20 chars for model
        
        # Create format string
        format_str = (f"%-{max_widths['device']}s  "
                     f"%-{max_widths['temperature']}s  "
                     f"%-{max_widths['current_sector']}s  "
                     f"%-{max_widths['supports_4k']}s  "
                     f"%-{max_widths['health']}s  "
                     f"%-{max_widths['wear_level']}s  "
                     f"%-{max_widths['power_hours']}s  "
                     f"%-{max_widths['error_count']}s  "
                     f"%-{max_widths['firmware']}s  "
                     f"%s")
        
        # Print header
        print(f"\n{format_str}" % ("Device", "Temp", "Current", "4K?", "Health", "Wear", "PowerHrs", "Errors", "Firmware", "Model"))
        print("-" * min(terminal_width - 1, sum(max_widths.values()) + model_width + 18))
        
        # Print devices
        for device in devices_info:
            model = device['model']
            if len(model) > model_width:
                model = model[:model_width-2] + '..'
            
            output = format_str % (
                device['device'],
                device['temperature'],
                self.format_current_sector(device['current_sector']),
                device['supports_4k'],
                device['health'],
                device['wear_level'],
                device['power_hours'],
                device['error_count'],
                device['firmware'],
                model
            )
            print(output)

    def run(self, show_all: bool = False):
        """Main execution function"""
        self.debug_print("Starting NVMe health analysis")
        
        # Discover NVMe devices
        devices = self.discover_nvme_devices()
        
        if not devices:
            print("No NVMe devices found in the system.")
            return
        
        self.debug_print(f"Analyzing {len(devices)} NVMe devices")
        
        # Check if we need elevated privileges and inform user (only if no sudo token)
        if not self.is_root():
            # Check if any devices are not readable
            unreadable_devices = []
            for device in devices:
                if not os.access(device, os.R_OK):
                    unreadable_devices.append(device)
            
            if unreadable_devices:
                self.debug_print(f"Non-root user cannot read {len(unreadable_devices)} devices: {unreadable_devices}")
                
                # Only show privilege escalation messages if user doesn't have sudo token
                has_token = self.has_sudo_token()
                self.debug_print(f"Sudo token available: {has_token}")
                
                if not has_token:
                    print("# NVMe health information requires elevated privileges for device access.")
                    print("# Will request sudo access when needed for nvme and smartctl commands...")
                else:
                    self.debug_print("User has valid sudo token - privilege escalation will be silent")
            else:
                self.debug_print("All devices are readable by current user")
        else:
            self.debug_print("Running as root - full device access available")
        
        # Get device information
        devices_info = []
        for device in devices:
            info = self.get_device_info(device)
            devices_info.append(info)
        
        # Display results
        self.display_devices(devices_info, show_all)
        
        self.debug_print("NVMe health analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="NVMe Device Health and Temperature Monitor - Shows NVMe devices with comprehensive health information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show NVMe devices with health information
  %(prog)s --all              # Show all NVMe devices (including those without health data)
  %(prog)s --debug            # Show with debug information

Column Information:
  Current   - Current sector size in use (512B, 4K, 520B, etc.)
  4K?       - Whether drive supports 4K sectors (Yes/No)
  
Note: By default, only NVMe devices with available health information are shown.
Use --all to show all discovered NVMe devices regardless of data availability.
This script will automatically request sudo privileges if needed to access
device health information through nvme and smartctl commands.
        """
    )

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details')
    parser.add_argument('--all', action='store_true', 
                       help='Show all NVMe devices (including those without health data)')
    parser.add_argument('--version', action='version', version=__version__,
                       help='Show program version and exit')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = NVMeHealthAnalyzer()
    analyzer.debug = args.debug

    # Run analysis
    analyzer.run(show_all=args.all)

if __name__ == "__main__":
    main()
