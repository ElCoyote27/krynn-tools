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
from collections import defaultdict

class HugePagesAnalyzer:
    def __init__(self):
        self.debug = False
        self.print_pattern = "%-38s %-10s : %s %s"
        self.index = 0
        self.grand_total_kb = 0

        # Hugepage sizes to analyze (size in KB, description)
        self.hugepage_sizes = [
            (2048, "2Mb HugePages"),
            (1048576, "1Gb HugePages")
        ]

        # KVM process names that should show guest names
        self.kvm_processes = ['qemu-kvm', 'qemu-system-x86_64', 'qemu-system-x86']

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def check_root_privileges(self):
        """Check if running as root, elevate privileges if needed"""
        if os.getuid() != 0:
            self.debug_print("Not running as root, attempting to elevate privileges with sudo")
            try:
                # Re-execute with sudo
                sudo_args = ['sudo'] + sys.argv
                self.debug_print(f"Executing: {' '.join(sudo_args)}")
                os.execvp('sudo', sudo_args)
            except (OSError, FileNotFoundError) as e:
                print(f"Error: Could not elevate privileges with sudo: {e}")
                print(f"Please run {sys.argv[0]} as root!")
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

    def extract_hugepage_info(self, smaps_files: List[str], kernel_page_size: int) -> Dict[str, int]:
        """Extract hugepage information from smaps files, grouped by PID"""
        pid_totals = defaultdict(int)

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
                                # Extract size from "Size:           2048 kB"
                                size_match = re.match(r'Size:\s+(\d+)\s+kB', lines[j])
                                if size_match:
                                    size_kb = int(size_match.group(1))
                                    pid_totals[pid] += size_kb
                                    self.debug_print(f"PID {pid}: +{size_kb} kB ({kernel_page_size} kB hugepages)")
                                break

            except (IOError, OSError):
                continue

        self.debug_print(f"Found {len(pid_totals)} processes using {kernel_page_size} kB hugepages")
        return dict(pid_totals)

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

    def format_process_info(self, pid: str, process_name: str, total_kb: int, kernel_page_size: int) -> Tuple[str, str, str, str]:
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

        # Format the size nicely
        formatted_size = self.format_size_display(total_kb)

        # Calculate number of hugepages used by this process
        num_hugepages = total_kb // kernel_page_size
        hugepage_display = f"{num_hugepages} hugepages"

        return display_name, f"({pid})", formatted_size, hugepage_display

    def format_size_display(self, size_kb: int) -> str:
        """Format size for consistent display"""
        if size_kb >= 1024 * 1024:  # >= 1GB
            size_gb = size_kb / (1024 * 1024)
            return f"{size_gb:.1f} GB"
        elif size_kb >= 1024:  # >= 1MB
            size_mb = size_kb / 1024
            return f"{size_mb:.1f} MB"
        else:
            return f"{size_kb} kB"

    def format_hugepage_size(self, size_kb: int) -> str:
        """Format hugepage size for headers"""
        if size_kb >= 1024 * 1024:  # >= 1GB
            size_gb = size_kb / (1024 * 1024)
            if size_gb == int(size_gb):
                return f"{int(size_gb)}GB"
            else:
                return f"{size_gb:.1f}GB"
        elif size_kb >= 1024:  # >= 1MB
            size_mb = size_kb / 1024
            if size_mb == int(size_mb):
                return f"{int(size_mb)}MB"
            else:
                return f"{size_mb:.1f}MB"
        else:
            return f"{size_kb}KB"

    def process_hugepage_size(self, kernel_page_size: int, description: str):
        """Process and display hugepage information for a specific size"""
        self.debug_print(f"=== Processing {description} ===")

        # Find processes using this hugepage size
        smaps_files = self.find_hugepage_processes(kernel_page_size)

        if not smaps_files:
            print(f"#### No {description} found!")
            return

        # Extract hugepage information (grouped by PID)
        pid_totals = self.extract_hugepage_info(smaps_files, kernel_page_size)

        if not pid_totals:
            print(f"#### No {description} found!")
            return

        # Format hugepage size for header
        hugepage_size_display = self.format_hugepage_size(kernel_page_size)

        print(self.print_pattern % (f"# [procname] ({hugepage_size_display} HugePages)", "PID", "Total HP", "Usage"))

        # Process and display results
        results = []
        section_total_kb = 0

        for pid_str, total_kb in pid_totals.items():
            # Check if process still exists
            if not os.path.exists(f'/proc/{pid_str}/status'):
                continue

            process_name = self.get_process_name(pid_str)
            if not process_name:
                continue

            display_name, pid_display, formatted_size, hugepage_display = self.format_process_info(
                pid_str, process_name, total_kb, kernel_page_size)

            result_line = self.print_pattern % (display_name, pid_display, formatted_size, hugepage_display)
            results.append((total_kb, result_line))  # Store with size for sorting
            section_total_kb += total_kb

        # Sort by total KB (descending)
        results.sort(key=lambda x: x[0], reverse=True)

        # Display results
        for _, result_line in results:
            print(result_line)

        # Display section total
        if results:
            print("-" * 60)
            section_total_display = self.format_size_display(section_total_kb)
            section_total_pages = section_total_kb // kernel_page_size
            print(self.print_pattern % (f"TOTAL ({hugepage_size_display})", "", 
                                       section_total_display, f"{section_total_pages} hugepages"))
            self.grand_total_kb += section_total_kb
            self.index = 1
            return section_total_kb  # Return section total for grand total calculation

        return 0

    def run(self):
        """Main execution function"""
        self.check_root_privileges()

        self.debug_print("Starting hugepage analysis")

        sections_with_data = 0

        # Process each hugepage size
        for kernel_page_size, description in self.hugepage_sizes:
            section_total = self.process_hugepage_size(kernel_page_size, description)
            if section_total and section_total > 0:
                sections_with_data += 1

        # Show grand total if we had multiple sections with data
        if sections_with_data > 1 and self.grand_total_kb > 0:
            print("=" * 60)
            grand_total_display = self.format_size_display(self.grand_total_kb)
            print(self.print_pattern % ("GRAND TOTAL (All HugePages)", "", grand_total_display, ""))

        # Format total for debug output
        total_display = self.format_size_display(self.grand_total_kb) if self.grand_total_kb > 0 else '0 kB'
        self.debug_print(f"Hugepage analysis completed: {total_display} total")

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
