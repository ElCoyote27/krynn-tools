#!/usr/bin/env python3
# $Id: CPU_temp.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of CPU_temp.sh - CPU temperature analyzer
# Improved socket/core grouping and better formatting

import os
import sys
import re
import subprocess
import argparse
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from collections import defaultdict
from dataclasses import dataclass

@dataclass
class CoreTemperature:
    """Represents a CPU core temperature reading"""
    socket: str              # CPU socket/package (e.g., "coretemp-isa-0000")
    core_id: str            # Core identifier (e.g., "Core 0")
    core_number: int        # Core number for sorting
    temperature: float      # Current temperature in Celsius
    temp_max: Optional[float]  # Maximum temperature if available
    temp_crit: Optional[float] # Critical temperature if available
    raw_line: str           # Original sensor line for reference

class CPUTemperatureAnalyzer:
    def __init__(self):
        self.debug = False
        self.show_details = False
        self.sort_by_temp = True  # Sort by temperature vs by core

        # Temperature collections
        self.core_temps: List[CoreTemperature] = []
        self.socket_groups: Dict[str, List[CoreTemperature]] = defaultdict(list)
        self.ambient_temp: Optional[str] = None

        # Tool paths
        self.sensors_cmd = '/usr/bin/sensors'
        self.ipmitool_cmd = '/usr/bin/ipmitool'
        self.ipmi_sensors_cmd = '/usr/sbin/ipmi-sensors'

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def check_tools(self) -> bool:
        """Check if required tools are available"""
        if not os.path.exists(self.sensors_cmd):
            print(f"{self.sensors_cmd} not found!")
            return False

        if not os.access(self.sensors_cmd, os.X_OK):
            print(f"{self.sensors_cmd} not executable!")
            return False

        return True

    def get_sensors_data(self) -> str:
        """Get sensors output data"""
        try:
            result = subprocess.run([self.sensors_cmd], capture_output=True, text=True, check=True)
            self.debug_print(f"sensors output: {len(result.stdout.splitlines())} lines")
            return result.stdout
        except subprocess.CalledProcessError as e:
            self.debug_print(f"sensors failed: {e}")
            return ""
        except Exception as e:
            self.debug_print(f"Error running sensors: {e}")
            return ""

    def parse_core_temperature_line(self, line: str, current_socket: str) -> Optional[CoreTemperature]:
        """Parse a single core temperature line from sensors output"""
        # Look for lines like: "Core 0:        +45.0°C  (high = +80.0°C, crit = +90.0°C)"
        core_match = re.match(r'Core\s+(\d+):\s*\+?(-?\d+(?:\.\d+)?)°C\s*(.*)', line.strip())
        if not core_match:
            return None

        core_number = int(core_match.group(1))
        temperature = float(core_match.group(2))
        additional_info = core_match.group(3)

        # Parse additional temperature info (high, crit, etc.)
        temp_max = None
        temp_crit = None

        if additional_info:
            high_match = re.search(r'high\s*=\s*\+?(-?\d+(?:\.\d+)?)°C', additional_info)
            crit_match = re.search(r'crit\s*=\s*\+?(-?\d+(?:\.\d+)?)°C', additional_info)

            if high_match:
                temp_max = float(high_match.group(1))
            if crit_match:
                temp_crit = float(crit_match.group(1))

        return CoreTemperature(
            socket=current_socket,
            core_id=f"Core {core_number}",
            core_number=core_number,
            temperature=temperature,
            temp_max=temp_max,
            temp_crit=temp_crit,
            raw_line=line.strip()
        )

    def parse_sensors_output(self, sensors_output: str):
        """Parse sensors output to extract CPU core temperatures"""
        lines = sensors_output.splitlines()
        current_socket = ""

        self.debug_print("Parsing sensors output...")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Detect socket/chip sections (e.g., "coretemp-isa-0000", "coretemp-isa-0001")
            if re.match(r'^[a-zA-Z][\w-]*-[a-zA-Z]+(-\d+)+$', line):
                current_socket = line
                self.debug_print(f"Found socket: {current_socket}")
                continue

            # Parse core temperature lines
            if line.startswith('Core ') and current_socket:
                core_temp = self.parse_core_temperature_line(line, current_socket)
                if core_temp:
                    self.core_temps.append(core_temp)
                    self.socket_groups[current_socket].append(core_temp)
                    self.debug_print(f"  {core_temp.core_id}: {core_temp.temperature}°C")

        self.debug_print(f"Found {len(self.core_temps)} core temperatures across {len(self.socket_groups)} sockets")

    def get_ambient_temperature(self) -> Optional[str]:
        """Get ambient temperature using IPMI if available, fallback to sensors output"""

        # First try IPMI method (only when running as root)
        if os.path.exists(self.ipmitool_cmd) and os.getuid() == 0:
            try:
                # Test if IPMI is available
                result = subprocess.run([self.ipmitool_cmd, 'sdr', 'info'],
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    # Try to get ambient temperature using ipmi-sensors
                    if os.path.exists(self.ipmi_sensors_cmd):
                        result = subprocess.run([self.ipmi_sensors_cmd, '-s', '10', '--ignore-not-available-sensors'],
                                              capture_output=True, text=True)
                        if result.returncode == 0:
                            for line in result.stdout.splitlines():
                                if 'Ambient' in line:
                                    parts = line.split('|')
                                    if len(parts) >= 5 and parts[1].strip().find('Ambient') != -1:
                                        temp_info = f"{parts[3].strip()} {parts[4].strip()}"
                                        self.debug_print(f"Found IPMI ambient temperature: {temp_info}")
                                        return temp_info
                else:
                    self.debug_print("IPMI not available")
            except Exception as e:
                self.debug_print(f"Error getting IPMI ambient temperature: {e}")
        elif not os.path.exists(self.ipmitool_cmd):
            self.debug_print("ipmitool not found")
        else:
            self.debug_print("IPMI requires root privileges, skipping (run with sudo for IPMI ambient temp)")

        # Fallback: Try to find ambient temperature in sensors output
        try:
            sensors_output = self.get_sensors_data()
            if sensors_output:
                ambient_temp = self.parse_ambient_from_sensors(sensors_output)
                if ambient_temp:
                    self.debug_print(f"Found sensors ambient temperature: {ambient_temp}")
                    return ambient_temp
        except Exception as e:
            self.debug_print(f"Error getting sensors ambient temperature: {e}")

        self.debug_print("No ambient temperature found")
        return None

    def get_ambient_temperature_with_data(self, sensors_output: str) -> Optional[str]:
        """Get ambient temperature using IPMI if available, fallback to provided sensors output"""

        # First try IPMI method (only when running as root)
        if os.path.exists(self.ipmitool_cmd) and os.getuid() == 0:
            try:
                # Test if IPMI is available
                result = subprocess.run([self.ipmitool_cmd, 'sdr', 'info'],
                                      capture_output=True, text=True)
                if result.returncode == 0:

                    # Try ipmitool sdr type temp for server ambient temperatures (Inlet, Ambient, etc.)
                    try:
                        result = subprocess.run([self.ipmitool_cmd, 'sdr', 'type', 'temp'],
                                              capture_output=True, text=True)
                        if result.returncode == 0:
                            for line in result.stdout.splitlines():
                                line = line.strip()
                                # Look for inlet, ambient, system, or board temperatures
                                if any(keyword in line.lower() for keyword in ['inlet temp', 'ambient temp', 'system temp', 'board temp']):
                                    # Parse ipmitool sdr output: "Sensor Name | ID | Status | Entity | Reading"
                                    if '|' in line:
                                        parts = line.split('|')
                                        if len(parts) >= 5:
                                            sensor_name = parts[0].strip()
                                            reading = parts[4].strip()
                                            if 'degrees C' in reading:
                                                # Extract temperature value
                                                temp_match = re.search(r'(\d+(?:\.\d+)?)', reading)
                                                if temp_match:
                                                    temp_value = temp_match.group(1)
                                                    temp_info = f"{temp_value}°C (IPMI {sensor_name})"
                                                    self.debug_print(f"Found IPMI ambient temperature: {temp_info}")
                                                    return temp_info
                    except Exception as e:
                        self.debug_print(f"Error with ipmitool sdr type temp: {e}")

                    # Fallback: Try to get ambient temperature using ipmi-sensors
                    if os.path.exists(self.ipmi_sensors_cmd):
                        result = subprocess.run([self.ipmi_sensors_cmd, '-s', '10', '--ignore-not-available-sensors'],
                                              capture_output=True, text=True)
                        if result.returncode == 0:
                            for line in result.stdout.splitlines():
                                if 'Ambient' in line:
                                    parts = line.split('|')
                                    if len(parts) >= 5 and parts[1].strip().find('Ambient') != -1:
                                        temp_info = f"{parts[3].strip()} {parts[4].strip()}"
                                        self.debug_print(f"Found IPMI ambient temperature: {temp_info}")
                                        return temp_info
                else:
                    self.debug_print("IPMI not available")
            except Exception as e:
                self.debug_print(f"Error getting IPMI ambient temperature: {e}")
        elif not os.path.exists(self.ipmitool_cmd):
            self.debug_print("ipmitool not found")
        else:
            self.debug_print("IPMI requires root privileges, skipping (run with sudo for IPMI ambient temp)")

        # Fallback: Try to find ambient temperature in provided sensors output
        try:
            if sensors_output:
                ambient_temp = self.parse_ambient_from_sensors(sensors_output)
                if ambient_temp:
                    self.debug_print(f"Found sensors ambient temperature: {ambient_temp}")
                    return ambient_temp
        except Exception as e:
            self.debug_print(f"Error getting sensors ambient temperature: {e}")

        self.debug_print("No ambient temperature found")
        return None

    def parse_ambient_from_sensors(self, sensors_output: str) -> Optional[str]:
        """Parse ambient temperature from sensors output"""
        lines = sensors_output.splitlines()
        current_adapter = ""

        # Look for potential ambient temperature sources in priority order
        ambient_candidates = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Track current adapter/sensor section
            if re.match(r'^[a-zA-Z][\w-]*-[a-zA-Z]+(-\d+)+$', line):
                current_adapter = line
                continue
            elif line.startswith('Adapter:'):
                continue

            # Skip CPU core temperatures and Package temps (already handled)
            if line.startswith('Core ') or line.startswith('Package id '):
                continue

            # Look for temperature readings
            temp_match = re.match(r'([^:]+):\s*\+?(-?\d+(?:\.\d+)?)°C', line)
            if temp_match:
                temp_name = temp_match.group(1).strip()
                temp_value = float(temp_match.group(2))
                temp_str = f"{temp_value}°C"

                # Prioritize certain adapters and temperature names for ambient
                priority = 0

                # Highest priority: ACPI thermal zones (often system ambient)
                if 'acpitz' in current_adapter or 'acpi' in current_adapter:
                    priority = 10
                    temp_str += " (ACPI Thermal Zone)"

                # High priority: Motherboard/chipset temperatures (but avoid PCH which runs hot)
                elif any(keyword in current_adapter.lower() for keyword in ['thinkpad', 'asus', 'msi', 'gigabyte', 'asrock']):
                    if temp_name.lower() in ['temp1', 'temp2', 'ambient', 'motherboard', 'system']:
                        priority = 8
                        temp_str += " (Motherboard)"

                # Lower priority: Chipset/PCH temperatures (usually run hotter than actual ambient)
                elif any(keyword in current_adapter.lower() for keyword in ['pch_', 'chipset', 'ich', 'fch']):
                    priority = 2
                    temp_str += " (Chipset)"

                # Medium priority: WiFi/network device temps (can indicate ambient)
                elif 'iwlwifi' in current_adapter or 'wifi' in current_adapter:
                    if temp_name == 'temp1':
                        priority = 5
                        temp_str += " (WiFi Sensor)"

                # Lower priority: Other generic temp sensors
                elif temp_name.lower() in ['temp1', 'ambient']:
                    priority = 3
                    temp_str += f" ({current_adapter})"

                if priority > 0 and 0 < temp_value < 80:  # Reasonable ambient temp range
                    ambient_candidates.append((priority, temp_str, temp_value))
                    self.debug_print(f"Found ambient candidate: {temp_str} (priority {priority})")

        # Return the highest priority ambient temperature
        if ambient_candidates:
            ambient_candidates.sort(key=lambda x: x[0], reverse=True)
            return ambient_candidates[0][1]

        return None

    def group_by_temperature(self) -> Dict[float, List[CoreTemperature]]:
        """Group cores by temperature"""
        temp_groups = defaultdict(list)
        for core in self.core_temps:
            temp_groups[core.temperature].append(core)
        return dict(temp_groups)

    def format_socket_name(self, socket: str, concise: bool = False) -> str:
        """Format socket name for display"""
        # Convert "coretemp-isa-0000" to "Socket 0", "coretemp-isa-0001" to "Socket 1", etc.
        match = re.search(r'-(\d+)$', socket)
        if match:
            socket_num = int(match.group(1))
            if concise:
                return f"P{socket_num}"
            else:
                return f"Socket {socket_num}"
        return socket

    def display_results_by_socket(self):
        """Display results grouped by socket, then by temperature"""
        print("\n=== CPU Temperature Report (Grouped by Socket) ===")

        # Sort sockets by name for consistent output
        sorted_sockets = sorted(self.socket_groups.keys())

        for socket in sorted_sockets:
            cores = self.socket_groups[socket]
            if not cores:
                continue

            socket_name = self.format_socket_name(socket, concise=False)
            print(f"\n{socket_name} ({socket}):")

            # Group cores by temperature within this socket
            temp_groups = defaultdict(list)
            for core in cores:
                temp_groups[core.temperature].append(core)

            # Sort temperatures (highest first to match original script behavior)
            sorted_temps = sorted(temp_groups.keys(), reverse=True)

            for temp in sorted_temps:
                temp_cores = temp_groups[temp]

                # Sort cores by core number
                temp_cores.sort(key=lambda x: x.core_number)

                # Get core numbers and format them
                core_numbers = [str(core.core_number) for core in temp_cores]
                core_list = ','.join(core_numbers)

                # Get additional temperature info (max/crit) from first core (should be same for all)
                temp_info = ""
                if temp_cores[0].temp_max or temp_cores[0].temp_crit:
                    temp_parts = []
                    if temp_cores[0].temp_max:
                        temp_parts.append(f"high = +{temp_cores[0].temp_max}°C")
                    if temp_cores[0].temp_crit:
                        temp_parts.append(f"crit = +{temp_cores[0].temp_crit}°C")
                    if temp_parts:
                        temp_info = f" ({', '.join(temp_parts)})"

                # Display the temperature group
                print(f"  Temp: {temp}°C{temp_info}, Cores: {core_list}")

    def display_results_by_temperature(self):
        """Display results grouped by temperature (original script style)"""
        print("\n=== CPU Temperature Report (Grouped by Temperature) ===")

        temp_groups = self.group_by_temperature()

        # Sort temperatures (highest first to match original script behavior)
        sorted_temps = sorted(temp_groups.keys(), reverse=True)

        for temp in sorted_temps:
            temp_cores = temp_groups[temp]

            # Group cores by socket for this temperature
            socket_cores = defaultdict(list)
            for core in temp_cores:
                socket_cores[core.socket].append(core)

            # Format core information with socket identification
            core_info_parts = []
            for socket in sorted(socket_cores.keys()):
                cores = socket_cores[socket]
                cores.sort(key=lambda x: x.core_number)
                core_numbers = [str(core.core_number) for core in cores]
                socket_name = self.format_socket_name(socket, concise=True)
                core_info_parts.append(f"{socket_name}:[{','.join(core_numbers)}]")

            core_info = ' '.join(core_info_parts)

            # Get additional temperature info (assuming all cores have same limits)
            temp_info = ""
            if temp_cores[0].temp_max or temp_cores[0].temp_crit:
                temp_parts = []
                if temp_cores[0].temp_max:
                    temp_parts.append(f"high = +{temp_cores[0].temp_max}°C")
                if temp_cores[0].temp_crit:
                    temp_parts.append(f"crit = +{temp_cores[0].temp_crit}°C")
                if temp_parts:
                    temp_info = f" ({', '.join(temp_parts)})"

            print(f"Temp: {temp}°C{temp_info}, CPU Cores: {core_info}")

    def display_detailed_info(self):
        """Display detailed information about all cores"""
        if not self.show_details:
            return

        print("\n=== Detailed Core Information ===")

        # Sort cores by socket, then by core number
        sorted_cores = sorted(self.core_temps, key=lambda x: (x.socket, x.core_number))

        print(f"{'Socket':<15} {'Core':<8} {'Temp':<8} {'Max':<8} {'Crit':<8} {'Raw Data'}")
        print("-" * 80)

        for core in sorted_cores:
            socket_name = self.format_socket_name(core.socket, concise=False)
            temp_max = f"{core.temp_max}°C" if core.temp_max else "N/A"
            temp_crit = f"{core.temp_crit}°C" if core.temp_crit else "N/A"

            print(f"{socket_name:<15} {core.core_id:<8} {core.temperature}°C{'':<2} {temp_max:<8} {temp_crit:<8} {core.raw_line}")

    def run_analysis(self):
        """Run the complete temperature analysis"""
        self.debug_print("Starting CPU temperature analysis")

        # Check if required tools are available
        if not self.check_tools():
            return 1

        # Get sensors data
        sensors_output = self.get_sensors_data()
        if not sensors_output:
            print("No sensors data available!")
            return 1

        # Parse the sensors output
        self.parse_sensors_output(sensors_output)

        if not self.core_temps:
            print("No CPU core temperatures found!")
            return 1

        # Get ambient temperature if available (pass sensors_output to avoid re-parsing)
        self.ambient_temp = self.get_ambient_temperature_with_data(sensors_output)

        # Display ambient temperature
        if self.ambient_temp:
            print(f"=== Ambient Temp: {self.ambient_temp}")

        # Display results
        if self.sort_by_temp:
            self.display_results_by_temperature()
        else:
            self.display_results_by_socket()

        # Show detailed information if requested
        self.display_detailed_info()

        self.debug_print("CPU temperature analysis completed")
        return 0

def main():
    parser = argparse.ArgumentParser(
        description="CPU temperature analyzer with improved socket/core grouping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show temperature groups (default)
  %(prog)s --by-socket        # Group by socket first, then temperature
  %(prog)s --details          # Show detailed core information
  %(prog)s --debug            # Show debug information
        """
    )

    parser.add_argument('--by-socket', action='store_true',
                       help='Group results by socket first, then by temperature')

    parser.add_argument('--details', action='store_true',
                       help='Show detailed information about all cores')

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = CPUTemperatureAnalyzer()
    analyzer.debug = args.debug
    analyzer.show_details = args.details
    analyzer.sort_by_temp = not args.by_socket  # Default is by temperature unless --by-socket specified

    # Run analysis
    try:
        return analyzer.run_analysis()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        if analyzer.debug:
            raise
        else:
            print(f"Error: {e}", file=sys.stderr)
            return 1

if __name__ == "__main__":
    sys.exit(main())
