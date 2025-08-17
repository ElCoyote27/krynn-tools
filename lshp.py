#!/usr/bin/env python3
# $Id: lshp.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of lshp - lists hugepages in use on Linux systems
# Shows guest names for KVM processes

import os
import sys
import re
import glob
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional

class HugePagesAnalyzer:
    def __init__(self):
        self.debug = False
        self.print_pattern = "%-38s %-10s : %s %s"
        self.index = 0
        
        # Hugepage sizes to analyze (size in KB, description)
        self.hugepage_sizes = [
            (2048, "2Mb HugePages"),
            (1048576, "1Gb HugePages")
        ]
        
        # KVM process names that should show guest names
        self.kvm_processes = ['qemu-kvm', 'qemu-system-x86']

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def check_root_privileges(self):
        """Check if running as root"""
        if os.getuid() != 0:
            print(f"Run {sys.argv[0]} as root!")
            sys.exit(127)

    def find_hugepage_processes(self, kernel_page_size: int) -> List[str]:
        """Find processes using specific hugepage size"""
        self.debug_print(f"Searching for processes using {kernel_page_size} kB hugepages")
        
        try:
            # Find all smaps files
            smaps_files = glob.glob('/proc/*/smaps')
            hugepage_processes = []
            
            for smaps_file in smaps_files:
                try:
                    with open(smaps_file, 'r') as f:
                        content = f.read()
                        if f'KernelPageSize:     {kernel_page_size} kB' in content:
                            hugepage_processes.append(smaps_file)
                except (IOError, OSError):
                    # Process may have disappeared or be inaccessible
                    continue
            
            self.debug_print(f"Found {len(hugepage_processes)} processes using {kernel_page_size} kB hugepages")
            return hugepage_processes
            
        except Exception as e:
            self.debug_print(f"Error finding hugepage processes: {e}")
            return []

    def extract_hugepage_info(self, smaps_files: List[str], kernel_page_size: int) -> List[Tuple[int, str, str, str]]:
        """Extract hugepage information from smaps files"""
        hugepage_info = []
        
        for smaps_file in smaps_files:
            try:
                with open(smaps_file, 'r') as f:
                    lines = f.readlines()
                
                # Extract PID from path: /proc/1234/smaps -> 1234
                pid_match = re.search(r'/proc/(\d+)/smaps', smaps_file)
                if not pid_match:
                    continue
                pid = pid_match.group(1)
                
                # Find KernelPageSize lines and extract the Size from 11 lines before
                for i, line in enumerate(lines):
                    if f'KernelPageSize:     {kernel_page_size} kB' in line:
                        # Look back up to 11 lines for Size entry
                        for j in range(max(0, i-11), i):
                            if lines[j].startswith('Size:'):
                                # Extract size and unit from "Size:           2048 kB"
                                size_match = re.match(r'Size:\s+(\d+)\s+(\w+)', lines[j])
                                if size_match:
                                    size = size_match.group(1)
                                    unit = size_match.group(2)
                                    hugepage_info.append((int(pid), pid, size, unit))
                                break
                
            except (IOError, OSError):
                continue
        
        # Remove duplicates and sort
        unique_info = list(set(hugepage_info))
        unique_info.sort()
        self.debug_print(f"Extracted {len(unique_info)} unique hugepage entries")
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
                        # Clean up guest name (remove debug options, guest= prefix)
                        guest_name = re.sub(r',debug.*', '', guest_name)
                        guest_name = re.sub(r'^guest=', '', guest_name)
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
                display_name = f"{process_name} [  ]"
                self.debug_print(f"KVM process {pid} ({process_name}) - no guest name found")
        else:
            display_name = f"{process_name} [  ]"
            
        return display_name, f"({pid})"

    def process_hugepage_size(self, kernel_page_size: int, description: str):
        """Process and display hugepage information for a specific size"""
        self.debug_print(f"=== Processing {description} ===")
        
        # Find processes using this hugepage size
        smaps_files = self.find_hugepage_processes(kernel_page_size)
        
        if not smaps_files:
            print(f"#### No {description} found!")
            return
        
        # Print header if this is the first hugepage type with results
        if self.index == 0:
            print(self.print_pattern % (f"# [procname] ({description})", "PID", "Size", "unit"))
        
        # Extract hugepage information
        hugepage_info = self.extract_hugepage_info(smaps_files, kernel_page_size)
        
        # Process and display results
        results = []
        for _, pid, size, unit in hugepage_info:
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
            
        if results:
            self.index = 1

    def run(self):
        """Main execution function"""
        self.check_root_privileges()
        
        self.debug_print("Starting hugepage analysis")
        
        # Process each hugepage size
        for kernel_page_size, description in self.hugepage_sizes:
            self.process_hugepage_size(kernel_page_size, description)
        
        self.debug_print("Hugepage analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="List hugepages in use on Linux systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show hugepage usage
  %(prog)s --debug            # Show with debug information
        """
    )
    
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')
    
    args = parser.parse_args()
    
    # Create and configure analyzer
    analyzer = HugePagesAnalyzer()
    analyzer.debug = args.debug
    
    # Run analysis
    analyzer.run()

if __name__ == "__main__":
    main()
