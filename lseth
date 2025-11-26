#!/usr/bin/env python3
# $Id: lseth.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of lseth - Network interface analyzer
# Shows physical and virtual interfaces with detailed information

import os
import sys
import re
import glob
import subprocess
import argparse
import shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Union

class NetworkInterfaceAnalyzer:
    def __init__(self):
        self.debug = False
        self.max_cols = None

        # Required tools
        self.required_tools = {
            'ip': '/sbin/ip',
            'ethtool': '/sbin/ethtool', 
            'lspci': '/usr/bin/lspci',
            'lsusb': '/usr/bin/lsusb'  # Optional
        }

        # Tool commands (will be populated)
        self.tools = {}

        # Interface type patterns
        self.physical_patterns = ['eth*', 'ib*', 'wl*', 'ww*', 'en*', 'sl*', 'em*', 'o*', 't*', 'p[0-9]*', 'q*', 'w*']

        # USB network drivers  
        self.usb_drivers = [
            'aqc111', 'asix', 'ax88179_178a', 'catc', 'cdc_eem', 'cdc_ether', 
            'cdc_mbim', 'cdc_ncm', 'cdc_subset', 'ch9200', 'cx82310_eth', 
            'dm9601', 'gl620a', 'hso', 'huawei_cdc_ncm', 'int51x1', 'ipheth', 
            'kalmia', 'kaweth', 'lan78xx', 'lg-vl600', 'mcs7830', 'net1080', 
            'pegasus', 'plusb', 'qmi_wwan', 'r8152', 'r8153_ecm', 'rndis_host', 
            'rtl8150', 'sierra_net', 'smsc75xx', 'smsc95xx', 'sr9700', 'usbnet', 'zaurus'
        ]

        # Drivers that benefit from detailed PCI description
        self.detailed_pci_drivers = ['ixgbe', 'sfc', 'e1000e', 'igb']

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def check_platform(self):
        """Check if running on Linux"""
        if os.uname().sysname != 'Linux':
            print(f"Not supported on {os.uname().sysname}! Exit!")
            sys.exit(125)

    def find_tools(self):
        """Locate required tools"""
        self.debug_print("Locating required tools...")

        for tool, default_path in self.required_tools.items():
            # Try default path first
            if os.path.exists(default_path) and os.access(default_path, os.X_OK):
                self.tools[tool] = default_path
                continue

            # Try to find in PATH
            tool_path = shutil.which(tool)
            if tool_path:
                self.tools[tool] = tool_path
                continue

            # Handle optional tools
            if tool == 'lsusb':
                self.debug_print(f"Optional tool {tool} not found")
                self.tools[tool] = None
                continue

            print(f"Unable to find '{tool}' in PATH! Aborting...")
            sys.exit(126)

        self.debug_print(f"Found tools: {self.tools}")

    def get_terminal_width(self):
        """Get terminal width for output formatting"""
        try:
            result = subprocess.run(['stty', 'size'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                _, cols = result.stdout.strip().split()
                self.max_cols = int(cols)
                self.debug_print(f"Terminal width: {self.max_cols}")
            else:
                self.max_cols = None
        except:
            self.max_cols = None

    def truncate_output(self, text: str) -> str:
        """Truncate output to terminal width if needed"""
        if self.max_cols and len(text) > self.max_cols:
            return text[:self.max_cols]
        return text

    def get_interface_list(self, interface_type: str) -> List[str]:
        """Get list of network interfaces"""
        if interface_type == 'physical':
            interfaces = []
            for pattern in self.physical_patterns:
                matches = glob.glob(f'/sys/class/net/{pattern}')
                interfaces.extend(matches)
            # Remove duplicates by converting to set, then back to list
            unique_interfaces = list(set(interfaces))
            return sorted([f for f in unique_interfaces if os.path.exists(f + '/device')])

        elif interface_type == 'virtual':
            # Check kernel version for virtual interface path
            kernel_version = os.uname().release.split('-')[0]
            if kernel_version.startswith('2.6.18'):
                vglob = '/sys/class/net/bond*'
            else:
                vglob = '/sys/devices/virtual/net/*'

            interfaces = glob.glob(vglob)
            return sorted([f for f in interfaces if os.path.exists(f + '/type')])

    def get_interface_state(self, interface: str) -> str:
        """Get interface state (up/down)"""
        try:
            result = subprocess.run([self.tools['ip'], '-o', 'l', 'sh', interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                if re.search(r'[!<,]UP[!>,]', result.stdout):
                    return 'up'
                else:
                    return '(down)'
        except:
            pass

        # Fallback to operstate file
        try:
            with open(f'/sys/class/net/{interface}/operstate', 'r') as f:
                return f.read().strip()
        except:
            return 'unknown'

    def get_interface_speed(self, interface: str, state: str) -> Union[str, int]:
        """Get interface speed"""
        if state != 'up' or interface.startswith('ib'):
            return 'N/A'

        # Try speed file first
        try:
            with open(f'/sys/class/net/{interface}/speed', 'r') as f:
                speed = int(f.read().strip())
                # Filter out invalid speeds (sometimes reports -1 or very large numbers)
                if speed <= 0 or speed >= 2000000000:
                    return 'N/A'
                return speed
        except:
            pass

        # Fallback to ethtool
        try:
            result = subprocess.run([self.tools['ethtool'], interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                match = re.search(r'Speed:\s+(\d+)Mb/s', result.stdout)
                if match:
                    return int(match.group(1))
        except:
            pass

        return 'N/A'

    def get_interface_buffers(self, interface: str) -> str:
        """Get TX/RX buffer sizes using ethtool -g, showing current/max"""
        if interface == 'lo' or interface.startswith('ib'):
            return 'N/A'

        try:
            result = subprocess.run([self.tools['ethtool'], '-g', interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5)

            if result.returncode == 0:
                lines = result.stdout.splitlines()

                # Parse both maximums and current values
                max_rx = max_tx = curr_rx = curr_tx = None
                in_max_section = False
                in_current_section = False

                for line in lines:
                    line = line.strip()

                    if 'Pre-set maximums:' in line:
                        in_max_section = True
                        in_current_section = False
                        continue
                    elif 'Current hardware settings:' in line:
                        in_max_section = False
                        in_current_section = True
                        continue

                    if in_max_section:
                        if line.startswith('RX:') and 'n/a' not in line.lower():
                            max_rx = line.split(':')[1].strip()
                        elif line.startswith('TX:') and 'n/a' not in line.lower():
                            max_tx = line.split(':')[1].strip()
                    elif in_current_section:
                        if line.startswith('RX:') and 'n/a' not in line.lower():
                            curr_rx = line.split(':')[1].strip()
                        elif line.startswith('TX:') and 'n/a' not in line.lower():
                            curr_tx = line.split(':')[1].strip()

                # Format as RX/TX pair for consistency
                if curr_rx and curr_tx:
                    return f"{curr_rx}/{curr_tx}"
                elif curr_rx:
                    return f"{curr_rx}/-"
                elif curr_tx:
                    return f"-/{curr_tx}"

            return 'N/A'

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            return 'N/A'

    def get_interface_mtu(self, interface: str) -> int:
        """Get interface MTU"""
        try:
            result = subprocess.run([self.tools['ip'], '-o', 'l', 'sh', interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                match = re.search(r'mtu (\d+)', result.stdout)
                if match:
                    return int(match.group(1))
        except:
            pass
        return 0

    def get_driver_info(self, interface: str) -> Tuple[str, str]:
        """Get driver name and PCI path"""
        try:
            result = subprocess.run([self.tools['ethtool'], '-i', interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                driver_match = re.search(r'driver:\s+(\S+)', result.stdout)
                bus_match = re.search(r'bus-info:\s+(\S+)', result.stdout)

                driver = driver_match.group(1) if driver_match else ''
                bus_info = bus_match.group(1) if bus_match else ''

                return driver, bus_info
        except:
            pass

        # Fallback methods
        driver = ''
        bus_info = ''

        try:
            # Get driver from sysfs
            driver_link = f'/sys/class/net/{interface}/device/driver/module'
            if os.path.islink(driver_link):
                driver = os.path.basename(os.readlink(driver_link))
        except:
            pass

        try:
            # Get bus info from sysfs
            device_link = f'/sys/class/net/{interface}/device'
            if os.path.islink(device_link):
                bus_info = os.path.basename(os.readlink(device_link))
        except:
            pass

        return driver, bus_info

    def get_mac_address(self, interface: str) -> str:
        """Get MAC address with bonding consideration"""
        mac = ''

        # Get current MAC
        try:
            with open(f'/sys/class/net/{interface}/address', 'r') as f:
                mac = f.read().strip()
        except:
            try:
                result = subprocess.run([self.tools['ip'], 'l', 'sh', interface], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    match = re.search(r'link/ether (\S+)', result.stdout)
                    if match:
                        mac = match.group(1)
            except:
                mac = 'N/A'

        # Check if interface is enslaved in bonding
        try:
            result = subprocess.run(['grep', '-H', f'Slave Interface: {interface}$', 
                                   '/proc/net/bonding/bond*'], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
            if result.returncode == 0:
                # Get the real MAC from bonding info
                bond_result = subprocess.run(['grep', '-A5', f'Slave Interface: {interface}$', 
                                           '/proc/net/bonding/bond*'], 
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
                if bond_result.returncode == 0:
                    hw_match = re.search(r'Permanent HW addr:\s+(\S+)', bond_result.stdout)
                    if hw_match:
                        real_mac = hw_match.group(1)
                        if real_mac != mac and real_mac:
                            mac = f'({real_mac})'
        except:
            pass

        # Handle InfiniBand addresses
        if interface.startswith('ib') and mac:
            # Extract the meaningful part of IB address
            ib_match = re.search(r'.*00:00:00:00:(.*)$', mac)
            if ib_match:
                mac = ib_match.group(1)

        return mac or 'N/A'

    def get_ip_address(self, interface: str, is_loopback: bool = False) -> str:
        """Get IP address"""
        try:
            if is_loopback:
                result = subprocess.run([self.tools['ip'], '-o', '-4', 'a', 's', interface], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    match = re.search(r'inet (\S+).*scope host', result.stdout)
                    if match:
                        return match.group(1)
            else:
                result = subprocess.run([self.tools['ip'], '-o', '-4', 'a', 's', interface], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    match = re.search(r'inet (\S+).*scope global(?!.*secondary)', result.stdout)
                    if match:
                        return match.group(1)
        except:
            pass

        return 'N/A'

    def get_device_description(self, driver: str, pci_path: str, interface: str) -> str:
        """Get device description"""
        if not pci_path:
            return 'N/A'

        desc = ''

        if driver in self.detailed_pci_drivers:
            # Get detailed subsystem info for these drivers
            try:
                result = subprocess.run([self.tools['lspci'], '-vmm', '-s', pci_path], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    vendor_match = re.search(r'SVendor:\s*(.+)', result.stdout)
                    device_match = re.search(r'SDevice:\s*(.+)', result.stdout)

                    if vendor_match and device_match:
                        vendor = vendor_match.group(1).strip()
                        device = device_match.group(1).strip()

                        if len(device) >= 16:  # MIN_DESCLEN
                            desc = f"{vendor} {device}"

            except:
                pass

        elif driver in self.usb_drivers:
            # Handle USB devices
            try:
                # Get USB device info
                result = subprocess.run(['udevadm', 'info', f'/sys/class/net/{interface}', '-x'], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    vid_match = re.search(r'ID_USB_VENDOR_ID=(\w+)', result.stdout)
                    pid_match = re.search(r'ID_USB_MODEL_ID=(\w+)', result.stdout)

                    if vid_match and pid_match and self.tools['lsusb']:
                        vid = vid_match.group(1)
                        pid = pid_match.group(1)

                        usb_result = subprocess.run([self.tools['lsusb'], '-d', f'{vid}:{pid}'], 
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                        if usb_result.returncode == 0:
                            desc_match = re.search(f'{vid}:{pid}\\s+(.+)', usb_result.stdout)
                            if desc_match:
                                desc = desc_match.group(1)
            except:
                desc = 'N/A'

        # Fallback to standard lspci
        if not desc:
            try:
                result = subprocess.run([self.tools['lspci'], '-D', '-s', pci_path], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    desc_match = re.search(r'.*:\s+(.+)', result.stdout)
                    if desc_match:
                        desc = desc_match.group(1)
            except:
                desc = 'N/A'

        return desc or 'N/A'

    def get_virtual_interface_info(self, interface: str) -> Dict:
        """Get virtual interface information"""
        info = {
            'slaves': '',
            'description': 'N/A'
        }

        # Check for bonding slaves
        bonding_slaves_file = f'/sys/class/net/{interface}/bonding/slaves'
        if os.path.exists(bonding_slaves_file):
            try:
                with open(bonding_slaves_file, 'r') as f:
                    slaves = f.read().strip().split()

                # Get active slave
                try:
                    with open(f'/proc/net/bonding/{interface}', 'r') as f:
                        bonding_info = f.read()
                        active_match = re.search(r'Currently Active Slave:\s*(\S+)', bonding_info)
                        active_slave = active_match.group(1) if active_match else ''
                except:
                    active_slave = ''

                # Format slaves list (active slave without parentheses, others with)
                # Sort to match shell script behavior (active slave first, then others sorted)
                formatted_slaves = []
                if active_slave and active_slave in slaves:
                    # Add active slave first
                    formatted_slaves.append(active_slave)
                    # Add remaining slaves in sorted order with parentheses
                    remaining = sorted([s for s in slaves if s != active_slave])
                    formatted_slaves.extend([f'({slave})' for slave in remaining])
                else:
                    # No active slave or not found, just sort all
                    formatted_slaves = [f'({slave})' for slave in sorted(slaves)]

                info['description'] = f"[ {' '.join(formatted_slaves)} ]"

            except:
                pass

        # Check for bridge slaves
        elif os.path.exists(f'/sys/class/net/{interface}/brif'):
            try:
                slaves = os.listdir(f'/sys/class/net/{interface}/brif')
                if slaves:
                    info['description'] = f"[ {' '.join(slaves)} ]"
            except:
                pass

        return info

    def get_virtual_driver_info(self, interface: str) -> str:
        """Get virtual driver information with version (cached)"""
        # Get base driver name
        try:
            result = subprocess.run([self.tools['ethtool'], '-i', interface], 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode == 0:
                driver_match = re.search(r'driver:\s+(\S+)', result.stdout)
                if driver_match:
                    driver = driver_match.group(1)
                else:
                    # Fallback: strip numbers from interface name
                    driver = re.sub(r'[0-9]+', '', interface)
            else:
                driver = re.sub(r'[0-9]+', '', interface)
        except:
            driver = re.sub(r'[0-9]+', '', interface)

        # Get driver version with caching (emulate shell script behavior)
        if not hasattr(self, 'driver_version_cache'):
            self.driver_version_cache = {}

        if driver in self.driver_version_cache:
            version = self.driver_version_cache[driver]
            self.debug_print(f"Using cached version for {driver}: {version}")
        else:
            # Try modinfo first
            version = ''
            try:
                result = subprocess.run(['/sbin/modinfo', '-F', 'version', driver], 
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode == 0:
                    version = result.stdout.strip()
                    self.debug_print(f"Got modinfo version for {driver}: '{version}'")
            except:
                self.debug_print(f"modinfo failed for {driver}")

            # If modinfo didn't give us a version, fallback to kernel version
            if not version:
                # Match shell script logic: uname -r with .el.* stripped
                kernel_release = os.uname().release
                if '.el' in kernel_release:
                    version = kernel_release.split('.el')[0]
                else:
                    version = kernel_release
                self.debug_print(f"Using kernel version fallback for {driver}: {version}")

            # Cache the result
            self.driver_version_cache[driver] = version

        # Format driver description and truncate if needed
        driver_desc = f"{driver} ({version})"
        if len(driver_desc) > 23:
            driver_desc = driver_desc[:22] + '..'

        return driver_desc

    def process_physical_interfaces(self):
        """Process and display physical interfaces with dynamic column widths"""
        interfaces = self.get_interface_list('physical')

        if not interfaces:
            self.debug_print("No physical interfaces found")
            return

        self.debug_print(f"Processing {len(interfaces)} physical interfaces")

        # First pass: collect all data and calculate column widths
        interface_data = []
        max_widths = {
            'name': len("#PHYS"),
            'state': len("STATE"), 
            'speed': len("SPEED"),
            'buffers': len("RX/TX"),
            'mtu': len("MTU"),
            'driver': len("DRIVER"),
            'hw_path': len("HW_Path"),
            'mac': len("MAC_Addr"),
            'ip': len("IP_Addr")
        }

        for interface_path in interfaces:
            interface = os.path.basename(interface_path)
            self.debug_print(f"Processing physical interface: {interface}")

            # Get interface information
            state = self.get_interface_state(interface)
            speed = str(self.get_interface_speed(interface, state))
            buffers = self.get_interface_buffers(interface)
            mtu = str(self.get_interface_mtu(interface))
            driver, pci_path = self.get_driver_info(interface)
            mac_addr = self.get_mac_address(interface)
            ip_addr = self.get_ip_address(interface)
            description = self.get_device_description(driver, pci_path, interface)

            # Store data
            row_data = {
                'name': interface,
                'state': state,
                'speed': speed,
                'buffers': buffers,
                'mtu': mtu,
                'driver': driver,
                'hw_path': pci_path,
                'mac': mac_addr,
                'ip': ip_addr,
                'description': description
            }
            interface_data.append(row_data)

            # Update maximum widths
            for key in max_widths:
                if key in row_data:
                    max_widths[key] = max(max_widths[key], len(str(row_data[key])))

        # Debug: show calculated column widths
        self.debug_print(f"Physical interface column widths: {max_widths}")

        # Create dynamic format pattern with proper spacing
        print_pattern = (f"%-{max_widths['name']}s  "
                        f"%-{max_widths['state']}s  "
                        f"%-{max_widths['speed']}s  "
                        f"%-{max_widths['buffers']}s  "
                        f"%{max_widths['mtu']}s  "
                        f"%-{max_widths['driver']}s  "
                        f"%-{max_widths['hw_path']}s  "
                        f"%-{max_widths['mac']}s  "
                        f"%-{max_widths['ip']}s  "
                        f"%s")

        # Print header
        print(f"\n{print_pattern}" % ("#PHYS", "STATE", "SPEED", "RX/TX", "MTU", "DRIVER", "HW_Path", "MAC_Addr", "IP_Addr", "Description"))

        # Print data
        for row in interface_data:
            output = print_pattern % (row['name'], row['state'], row['speed'], row['buffers'], 
                                    row['mtu'], row['driver'], row['hw_path'], row['mac'], 
                                    row['ip'], row['description'])
            print(self.truncate_output(output))

    def process_virtual_interfaces(self):
        """Process and display virtual interfaces with dynamic column widths"""
        interfaces = self.get_interface_list('virtual')

        if not interfaces:
            self.debug_print("No virtual interfaces found")
            return

        self.debug_print(f"Processing {len(interfaces)} virtual interfaces")

        # First pass: collect all data and calculate column widths
        interface_data = []
        max_widths = {
            'name': len("#VIRT"),
            'state': len("STATE"),
            'buffers': len("RX/TX"),
            'mtu': len("MTU"),
            'driver': len("DRIVER"),
            'mac': len("Active MAC"),
            'ip': len("IP_Addr")
        }

        for interface_path in interfaces:
            interface = os.path.basename(interface_path)

            # Check if interface has a type (skip if not)
            try:
                with open(f'{interface_path}/type', 'r') as f:
                    if not f.read().strip():
                        continue
            except:
                continue

            self.debug_print(f"Processing virtual interface: {interface}")

            # Get interface information
            state = self.get_interface_state(interface)
            buffers = self.get_interface_buffers(interface)
            mtu = str(self.get_interface_mtu(interface))
            driver = self.get_virtual_driver_info(interface)
            mac_addr = self.get_mac_address(interface)
            ip_addr = self.get_ip_address(interface, is_loopback=(interface == 'lo'))

            # Get virtual interface specific info
            virt_info = self.get_virtual_interface_info(interface)
            description = virt_info['description']

            # Store data
            row_data = {
                'name': interface,
                'state': state,
                'buffers': buffers,
                'mtu': mtu,
                'driver': driver,
                'mac': mac_addr,
                'ip': ip_addr,
                'description': description
            }
            interface_data.append(row_data)

            # Update maximum widths
            for key in max_widths:
                if key in row_data:
                    max_widths[key] = max(max_widths[key], len(str(row_data[key])))

        # Debug: show calculated column widths
        self.debug_print(f"Virtual interface column widths: {max_widths}")

        # Create dynamic format pattern with proper spacing
        virt_pattern = (f"%-{max_widths['name']}s  "
                       f"%-{max_widths['state']}s  "
                       f"%-{max_widths['buffers']}s  "
                       f"%{max_widths['mtu']}s  "
                       f"%-{max_widths['driver']}s  "
                       f"%-{max_widths['mac']}s  "
                       f"%-{max_widths['ip']}s  "
                       f"%s")

        # Print header
        print(f"\n{virt_pattern}" % ("#VIRT", "STATE", "RX/TX", "MTU", "DRIVER", "Active MAC", "IP_Addr", "Description"))

        # Print data
        for row in interface_data:
            output = virt_pattern % (row['name'], row['state'], row['buffers'], row['mtu'], 
                                   row['driver'], row['mac'], row['ip'], row['description'])
            print(self.truncate_output(output))

    def run(self):
        """Main execution function"""
        self.check_platform()
        self.find_tools()
        self.get_terminal_width()

        self.debug_print("Starting network interface analysis")

        # Process physical interfaces
        self.process_physical_interfaces()

        # Process virtual interfaces  
        self.process_virtual_interfaces()

        self.debug_print("Network interface analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="Network interface analyzer for Linux systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show all network interfaces
  %(prog)s --debug            # Show with debug information
        """
    )

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = NetworkInterfaceAnalyzer()
    analyzer.debug = args.debug

    # Run analysis
    analyzer.run()

if __name__ == "__main__":
    main()
