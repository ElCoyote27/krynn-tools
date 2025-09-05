#!/usr/bin/env python3
#
# $Id: lsPCISpeeds.py,v 1.09 2025/09/05 12:00:00 python36-compatibility-fix Exp $
#
# PCI Device Speed Analyzer
# Shows PCI devices with max speeds, negotiated speeds, and lane configuration
# Uses lspci -vvv output to extract PCI Express capability information

__version__ = "lsPCISpeeds.py 1.09 2025/09/05 12:00:00 python36-compatibility-fix Exp"

#
# VERSION HISTORY:
# ================
#
# v1.09 (2025-09-05): Python 3.6+ compatibility fix
#   - Fixed subprocess.run() capture_output parameter for Python 3.6 compatibility
#   - Fixed subprocess.run() text=True parameter (replaced with universal_newlines=True)
#   - Replaced capture_output=True with stdout/stderr=subprocess.PIPE for RHEL8 support
#   - Now fully compatible with Python 3.6+ (tested on RHEL8 Python 3.6)
#
# v1.08 (2025-09-02): Enhanced downgrade detection
#   - Added lane downgrade detection (x16 max running at x8/x4, etc.)
#   - Added lspci verification by checking for explicit downgrade indicators
#   - Stores raw lspci output per device for pattern matching
#   - More accurate identification of performance bottlenecks
#
# v1.07 (2025-09-02): Fixed piped output truncation
#   - Detected when output is piped/redirected and use generous width (200 chars)
#   - Prevents description truncation when using grep, less, or other pipe commands
#   - Terminal output still uses actual terminal width for optimal display
#
# v1.06 (2025-09-02): Code cleanup and whitespace normalization
#   - Cleaned up any lines containing only whitespace characters
#   - Replaced whitespace-only lines with proper blank lines for consistency
#   - Improved code formatting standards matching other tools in the suite
#
# v1.05 (2025-09-02): Added downgraded device filtering
#   - Added --downgraded option to show only devices running slower than max speed
#   - Implemented speed parsing and comparison logic for performance analysis
#   - Perfect for identifying bottlenecks where devices aren't reaching full potential
#   - Enhanced help text with performance troubleshooting guidance
#
# v1.04 (2025-09-02): Simplified command-line options
#   - Removed redundant --full option
#   - Made --all truly show everything (all device types and N/A speeds)
#   - Cleaner, less confusing interface with only two modes: default and --all
#
# v1.03 (2025-09-02): Added full device filtering option
#   - Added --full option to show devices with N/A speeds
#   - Enhanced filtering logic for better control over output
#   - Improved help documentation with clearer examples
#
# v1.02 (2025-09-02): Clean device descriptions
#   - Fixed parsing of revision and prog-if information
#   - Removes clutter like "(rev 11)" and "(prog-if 00 [Normal decode])"
#   - Cleaner, more readable device descriptions
#
# v1.01 (2025-09-02): Improved output formatting
#   - Added device type abbreviations (VGA, NVMe, Bridge, Ethernet, etc.)
#   - Enhanced terminal width detection and dynamic column sizing
#   - Better handling of long device descriptions
#   - Reduced output truncation issues
#
# v1.00 (2025-09-02): Initial release
#   - PCI Express speed and lane analysis from lspci -vvv output
#   - Automatic privilege escalation when needed
#   - Extracts max speeds, negotiated speeds, and lane widths
#   - Concise tabular output format
#   - Debug mode and version information
#

import os
import sys
import re
import subprocess
import argparse
import shutil
from typing import List, Dict, Tuple, Optional

class PCISpeedAnalyzer:
    def __init__(self):
        self.debug = False
        self.lspci_path = None

        # Device type abbreviations for cleaner output
        self.device_type_abbrev = {
            'VGA compatible controller': 'VGA',
            'Non-Volatile memory controller': 'NVMe',
            'PCI bridge': 'Bridge',
            'Ethernet controller': 'Ethernet',
            'Network controller': 'Network',
            'Signal processing controller': 'Signal Proc',
            'USB controller': 'USB',
            'SATA controller': 'SATA',
            'Audio device': 'Audio',
            'Encryption controller': 'Crypto',
            'Non-Essential Instrumentation [1300]': 'Instrumentation',
            'System peripheral': 'System',
            'Communication controller': 'Comm',
            'Multimedia controller': 'Multimedia',
            'Memory controller': 'Memory',
            'Serial bus controller': 'Serial Bus'
        }

        # Find lspci tool
        self.find_lspci()

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def find_lspci(self):
        """Locate lspci tool"""
        # Try common paths
        common_paths = ['/usr/bin/lspci', '/sbin/lspci', '/usr/sbin/lspci']

        for path in common_paths:
            if os.path.exists(path) and os.access(path, os.X_OK):
                self.lspci_path = path
                return

        # Try to find in PATH
        lspci_path = shutil.which('lspci')
        if lspci_path:
            self.lspci_path = lspci_path
            return

        print("Unable to find 'lspci' command! Aborting...")
        sys.exit(126)

    def is_root(self) -> bool:
        """Check if running as root"""
        return os.geteuid() == 0

    def run_lspci(self) -> str:
        """Run lspci -vvv to get detailed PCI information"""
        # Try regular user first
        self.debug_print("Trying lspci without elevated privileges...")
        try:
            result = subprocess.run([self.lspci_path, '-vvv'], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
            if result.returncode == 0:
                # Check if we got PCI Express capability information
                if 'LnkCap:' in result.stdout and 'LnkSta:' in result.stdout:
                    self.debug_print("Got PCI Express capabilities without sudo")
                    return result.stdout
                elif not self.is_root():
                    # We didn't get Express capabilities and we're not root
                    self.debug_print("No PCI Express capabilities found, trying with sudo...")
                    print("# PCI Express capabilities require elevated privileges.")
                    print("# Requesting sudo access to read full PCI configuration...")

                    try:
                        result = subprocess.run(['sudo', self.lspci_path, '-vvv'], 
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)  # Allow more time for sudo password
                        if result.returncode == 0:
                            self.debug_print("Got enhanced PCI data with sudo")
                            return result.stdout
                    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                        print("# Failed to run lspci with sudo, falling back to limited data")
                        pass

                # Return what we have, even if limited
                return result.stdout
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass

        print("Failed to run lspci command!")
        sys.exit(1)

    def parse_pci_devices(self, lspci_output: str) -> List[Dict]:
        """Parse lspci output to extract device information"""
        devices = []
        current_device = None
        in_express_capability = False
        current_device_lines = []

        lines = lspci_output.split('\n')

        for line in lines:
            line = line.rstrip()

            # Check for new device (starts at beginning of line with bus:device.function)
            device_match = re.match(r'^([0-9a-fA-F:\.]+)\s+(.+)', line)
            if device_match:
                # Save previous device if it exists
                if current_device:
                    current_device['raw_output'] = '\n'.join(current_device_lines)
                    devices.append(current_device)

                # Start new device
                pci_address = device_match.group(1)
                description = device_match.group(2)

                current_device = {
                    'pci_address': pci_address,
                    'description': description,
                    'max_speed': 'N/A',
                    'max_lanes': 'N/A', 
                    'cur_speed': 'N/A',
                    'cur_lanes': 'N/A',
                    'has_express': False,
                    'raw_output': ''
                }
                current_device_lines = [line]
                in_express_capability = False
                continue

            # Store all lines for this device
            if current_device:
                current_device_lines.append(line)

            if not current_device:
                continue

            # Check for Express capability
            if 'Capabilities:' in line and 'Express' in line:
                current_device['has_express'] = True
                in_express_capability = True
                continue

            # Look for new capability section (resets express flag)
            if line.startswith('\tCapabilities:') and 'Express' not in line:
                in_express_capability = False
                continue

            # Parse Express capability information
            if in_express_capability:
                # Parse LnkCap (Link Capabilities - maximum speeds)
                lnkcap_match = re.search(r'LnkCap:.*Speed ([0-9.]+GT/s).*Width (x\d+)', line)
                if lnkcap_match:
                    current_device['max_speed'] = lnkcap_match.group(1)
                    current_device['max_lanes'] = lnkcap_match.group(2)
                    continue

                # Parse LnkSta (Link Status - current negotiated speeds)  
                lnksta_match = re.search(r'LnkSta:.*Speed ([0-9.]+GT/s).*Width (x\d+)', line)
                if lnksta_match:
                    current_device['cur_speed'] = lnksta_match.group(1)
                    current_device['cur_lanes'] = lnksta_match.group(2)
                    continue

        # Don't forget the last device
        if current_device:
            current_device['raw_output'] = '\n'.join(current_device_lines)
            devices.append(current_device)

        return devices

    def format_device_description(self, description: str, max_length: int = 80) -> str:
        """Format and truncate device description with type abbreviations"""
        # Apply device type abbreviations
        for full_type, abbrev in self.device_type_abbrev.items():
            if description.startswith(full_type + ':'):
                description = abbrev + ':' + description[len(full_type)+1:]
                break

        # Clean up revision and prog-if information
        # Remove patterns like "(rev 11)", "(prog-if 00 [Normal decode])", etc.
        import re
        description = re.sub(r'\s*\(rev \w+\)', '', description)
        description = re.sub(r'\s*\(prog-if [^)]+\)', '', description)

        # Clean up any double spaces that might result
        description = re.sub(r'\s+', ' ', description).strip()

        if len(description) <= max_length:
            return description
        return description[:max_length-2] + '..'

    def filter_express_devices(self, devices: List[Dict]) -> List[Dict]:
        """Filter to only include devices with PCI Express capabilities"""
        return [device for device in devices if device['has_express']]

    def filter_devices_with_speeds(self, devices: List[Dict]) -> List[Dict]:
        """Filter to only include devices with at least some speed information"""
        return [device for device in devices 
                if device['max_speed'] != 'N/A' or device['cur_speed'] != 'N/A']

    def parse_speed_value(self, speed_str: str) -> float:
        """Parse speed string to numeric value for comparison (e.g., '16GT/s' -> 16.0)"""
        if speed_str == 'N/A':
            return 0.0
        # Extract numeric part from strings like "16GT/s", "8GT/s", "2.5GT/s"
        import re
        match = re.match(r'([0-9.]+)', speed_str)
        if match:
            return float(match.group(1))
        return 0.0

    def parse_lane_value(self, lane_str: str) -> int:
        """Parse lane string to numeric value for comparison (e.g., 'x16' -> 16)"""
        if lane_str == 'N/A':
            return 0
        # Extract numeric part from strings like "x16", "x8", "x4", "x1"
        import re
        match = re.match(r'x(\d+)', lane_str)
        if match:
            return int(match.group(1))
        return 0

    def check_lspci_downgrade_indicators(self, raw_output: str) -> bool:
        """Check if lspci output contains indicators of downgrading"""
        # Look for common downgrade indicators in lspci output
        downgrade_patterns = [
            r'downgraded',
            r'Width.*downgraded',
            r'Speed.*downgraded', 
            r'training.*failed',
            r'link.*train.*error',
            r'negotiat.*fail',
            r'Width.*x\d+.*\(downgraded\)',
            r'Speed.*GT/s.*\(downgraded\)'
        ]

        for pattern in downgrade_patterns:
            if re.search(pattern, raw_output, re.IGNORECASE):
                return True
        return False

    def filter_downgraded_devices(self, devices: List[Dict]) -> List[Dict]:
        """Filter to only include devices where negotiated < max (speed or lanes) or lspci indicates downgrade"""
        downgraded = []
        for device in devices:
            # Skip devices without speed information
            if device['max_speed'] == 'N/A' or device['cur_speed'] == 'N/A':
                continue

            max_speed = self.parse_speed_value(device['max_speed'])
            cur_speed = self.parse_speed_value(device['cur_speed'])
            max_lanes = self.parse_lane_value(device['max_lanes'])
            cur_lanes = self.parse_lane_value(device['cur_lanes'])

            is_downgraded = False

            # Check speed downgrading
            if max_speed > 0 and cur_speed > 0 and cur_speed < max_speed:
                is_downgraded = True

            # Check lane downgrading
            if max_lanes > 0 and cur_lanes > 0 and cur_lanes < max_lanes:
                is_downgraded = True

            # Check if lspci explicitly mentions downgrading
            if self.check_lspci_downgrade_indicators(device.get('raw_output', '')):
                is_downgraded = True

            if is_downgraded:
                downgraded.append(device)

        return downgraded

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
                result = subprocess.run(['stty', 'size'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    _, cols = result.stdout.strip().split()
                    return int(cols)
            except:
                pass
        return 120  # Default fallback for terminal

    def display_devices(self, devices: List[Dict], show_all: bool = False, show_downgraded: bool = False):
        """Display PCI devices in a formatted table"""

        # Apply filtering based on options
        if show_downgraded:
            # Show only downgraded devices (Express devices where cur_speed < max_speed)
            devices = self.filter_express_devices(devices)
            devices = self.filter_downgraded_devices(devices)
        elif not show_all:
            # Default: Express devices with speed information only
            devices = self.filter_express_devices(devices)
            devices = self.filter_devices_with_speeds(devices)
        # If show_all is True, show everything (no filtering)

        if not devices:
            if show_downgraded:
                print("No downgraded PCI Express devices found.")
            elif show_all:
                print("No PCI devices found.")
            else:
                print("No PCI Express devices found with speed information.")
            return

        # Get terminal width and calculate description width
        terminal_width = self.get_terminal_width()

        # Calculate column widths
        max_widths = {
            'address': max(len('PCI_Address'), max(len(d['pci_address']) for d in devices)),
            'max_speed': max(len('Max_Speed'), max(len(str(d['max_speed'])) for d in devices)),
            'max_lanes': max(len('Max_Lanes'), max(len(str(d['max_lanes'])) for d in devices)),
            'cur_speed': max(len('Cur_Speed'), max(len(str(d['cur_speed'])) for d in devices)),
            'cur_lanes': max(len('Cur_Lanes'), max(len(str(d['cur_lanes'])) for d in devices)),
        }

        # Calculate remaining space for description
        used_width = sum(max_widths.values()) + 10  # 10 for spacing between columns
        desc_width = max(30, terminal_width - used_width - 5)  # Minimum 30 chars for description

        # Create format string
        format_str = (f"%-{max_widths['address']}s  "
                     f"%-{max_widths['max_speed']}s  "
                     f"%-{max_widths['max_lanes']}s  "
                     f"%-{max_widths['cur_speed']}s  "
                     f"%-{max_widths['cur_lanes']}s  "
                     f"%s")

        # Print header
        print(f"\n{format_str}" % ("PCI_Address", "Max_Speed", "Max_Lanes", "Cur_Speed", "Cur_Lanes", "Description"))
        print("-" * min(terminal_width - 1, sum(max_widths.values()) + desc_width + 10))

        # Print devices
        for device in devices:
            description = self.format_device_description(device['description'], desc_width)
            output = format_str % (
                device['pci_address'],
                device['max_speed'], 
                device['max_lanes'],
                device['cur_speed'],
                device['cur_lanes'],
                description
            )
            print(output)

    def run(self, show_all: bool = False, show_downgraded: bool = False):
        """Main execution function"""
        self.debug_print("Starting PCI speed analysis")

        # Get lspci output with automatic privilege escalation if needed
        lspci_output = self.run_lspci()
        self.debug_print(f"Got {len(lspci_output)} characters of lspci output")

        # Parse devices
        devices = self.parse_pci_devices(lspci_output)
        self.debug_print(f"Found {len(devices)} total PCI devices")

        # Display results
        self.display_devices(devices, show_all, show_downgraded)

        self.debug_print("PCI speed analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="PCI Device Speed Analyzer - Shows PCI Express devices with speed and lane information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show PCI Express devices with speed info
  %(prog)s --all              # Show all PCI devices (including non-Express and N/A speeds)
  %(prog)s --downgraded       # Show only devices with speed/lane downgrades
  %(prog)s --debug            # Show with debug information

Note: By default, only PCI Express devices with speed information are shown.
Use --all to show everything, or --downgraded to find performance bottlenecks.
This script will automatically request sudo privileges if needed to access
full PCI Express capability information.
        """
    )

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details')
    parser.add_argument('--all', action='store_true', 
                       help='Show all PCI devices (including non-Express and N/A speeds)')
    parser.add_argument('--downgraded', action='store_true',
                       help='Show only devices where negotiated speed < max speed')
    parser.add_argument('--version', action='version', version=__version__,
                       help='Show program version and exit')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = PCISpeedAnalyzer()
    analyzer.debug = args.debug

    # Run analysis
    analyzer.run(show_all=args.all, show_downgraded=args.downgraded)

if __name__ == "__main__":
    main()
