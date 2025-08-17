#!/usr/bin/env python3
# $Id: lsthp.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of lsthp - lists transparent hugepages in use on Linux systems
# Shows guest names for KVM processes

import os
import sys
import re
import glob
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional

class TransparentHugePagesAnalyzer:
    def __init__(self):
        self.debug = False
        self.print_pattern = "%-24s %-10s : %s %s"
        self.index = 0
        
        # KVM process names that should show guest names
        self.kvm_processes = ['qemu-kvm']

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def check_root_privileges(self):
        """Check if running as root"""
        if os.getuid() != 0:
            print(f"Run {sys.argv[0]} as root!")
            sys.exit(127)

    def find_transparent_hugepage_processes(self) -> List[str]:
        """Find processes using transparent hugepages"""
        self.debug_print("Searching for processes using transparent hugepages")
        
        try:
            # Find all smaps files
            smaps_files = glob.glob('/proc/*/smaps')
            thp_processes = []
            
            for smaps_file in smaps_files:
                try:
                    with open(smaps_file, 'r') as f:
                        content = f.read()
                        # Look for AnonHugePages entries that are not "0 kB"
                        if 'AnonHugePages:' in content:
                            lines = content.split('\n')
                            for line in lines:
                                if 'AnonHugePages:' in line and not line.endswith(' 0 kB'):
                                    thp_processes.append(smaps_file)
                                    break
                except (IOError, OSError):
                    # Process may have disappeared or be inaccessible
                    continue
            
            self.debug_print(f"Found {len(thp_processes)} processes using transparent hugepages")
            return thp_processes
            
        except Exception as e:
            self.debug_print(f"Error finding transparent hugepage processes: {e}")
            return []

    def extract_thp_info(self, smaps_files: List[str]) -> List[Tuple[int, str, str, str]]:
        """Extract transparent hugepage information from smaps files"""
        thp_info = []
        
        for smaps_file in smaps_files:
            try:
                with open(smaps_file, 'r') as f:
                    content = f.read()
                
                # Extract PID from path: /proc/1234/smaps -> 1234
                pid_match = re.search(r'/proc/(\d+)/smaps', smaps_file)
                if not pid_match:
                    continue
                pid = pid_match.group(1)
                
                # Find all AnonHugePages lines that are not "0 kB"
                for line in content.split('\n'):
                    if 'AnonHugePages:' in line and not line.endswith(' 0 kB'):
                        # Extract size and unit from "AnonHugePages:    2048 kB"
                        anon_match = re.search(r'AnonHugePages:\s+(\d+)\s+(\w+)', line)
                        if anon_match:
                            size = anon_match.group(1)
                            unit = anon_match.group(2)
                            if size != '0':  # Double-check it's not zero
                                thp_info.append((int(size), pid, size, unit))
                
            except (IOError, OSError):
                continue
        
        # Sort by size (numeric) and remove duplicates
        unique_info = list(set(thp_info))
        unique_info.sort()
        self.debug_print(f"Extracted {len(unique_info)} unique transparent hugepage entries")
        return unique_info

    def get_process_name(self, pid: str) -> Optional[str]:
        """Get process name from /proc/PID/status"""
        try:
            with open(f'/proc/{pid}/status', 'r') as f:
                for line in f:
                    if line.startswith('Name:'):
                        return line.split()[1]
        except (IOError, OSError):
            pass
        return None

    def get_guest_name(self, pid: str) -> str:
        """Extract guest name from KVM process cmdline"""
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline_bytes = f.read()
                # Convert null-separated cmdline to string list
                cmdline_parts = cmdline_bytes.decode('utf-8', errors='ignore').split('\0')
                
                # Look for -name argument
                for i, part in enumerate(cmdline_parts):
                    if part == '-name' and i + 1 < len(cmdline_parts):
                        guest_name = cmdline_parts[i + 1]
                        return guest_name
                        
        except (IOError, OSError):
            pass
        return ""

    def format_process_info(self, pid: str, process_name: str, size: str, unit: str) -> Tuple[str, str]:
        """Format process information with guest name if applicable"""
        if process_name in self.kvm_processes:
            guest_name = self.get_guest_name(pid)
            if guest_name:
                display_name = f"{process_name} [ {guest_name} ]"
                self.debug_print(f"KVM process {pid} ({process_name}) guest: {guest_name}")
            else:
                display_name = process_name
                self.debug_print(f"KVM process {pid} ({process_name}) - no guest name found")
        else:
            display_name = process_name
            
        return display_name, f"({pid})"

    def run(self):
        """Main execution function"""
        self.check_root_privileges()
        
        self.debug_print("Starting transparent hugepage analysis")
        
        # Find processes using transparent hugepages
        smaps_files = self.find_transparent_hugepage_processes()
        
        if not smaps_files:
            print("No Transparent HugePages found!")
            return
        
        # Print header
        if self.index == 0:
            print(self.print_pattern % ("# [procname]", "PID", "Size", "unit"))
        
        # Extract transparent hugepage information
        thp_info = self.extract_thp_info(smaps_files)
        
        # Process and display results
        results = []
        for _, pid, size, unit in thp_info:
            pid_str = str(pid)
            
            # Check if process still exists
            if not os.path.exists(f'/proc/{pid_str}/status'):
                continue
                
            process_name = self.get_process_name(pid_str)
            if not process_name:
                continue
                
            display_name, pid_display = self.format_process_info(pid_str, process_name, size, unit)
            result_line = self.print_pattern % (display_name, pid_display, size, unit)
            results.append(result_line)
        
        # Remove duplicates and sort
        unique_results = sorted(set(results))
        for result in unique_results:
            print(result)
            
        self.debug_print("Transparent hugepage analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="List transparent hugepages in use on Linux systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show transparent hugepage usage
  %(prog)s --debug            # Show with debug information
        """
    )
    
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')
    
    args = parser.parse_args()
    
    # Create and configure analyzer
    analyzer = TransparentHugePagesAnalyzer()
    analyzer.debug = args.debug
    
    # Run analysis
    analyzer.run()

if __name__ == "__main__":
    main()
