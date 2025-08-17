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
from collections import defaultdict

class TransparentHugePagesAnalyzer:
    def __init__(self):
        self.debug = False
        self.print_pattern = "%-24s %-10s : %s %s"
        self.index = 0

        # KVM process names that should show guest names
        self.kvm_processes = ['qemu-kvm', 'qemu-system-x86_64', 'qemu-system-x86']

        # Hugepage size detection
        self.hugepage_size_kb = self.get_hugepage_size()
        self.hugepage_size_display = self.format_hugepage_size(self.hugepage_size_kb)

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def get_hugepage_size(self) -> int:
        """Get transparent hugepage size in KB"""
        try:
            # Try to read from /sys/kernel/mm/transparent_hugepage/hpage_pmd_size
            hpage_file = '/sys/kernel/mm/transparent_hugepage/hpage_pmd_size'
            if os.path.exists(hpage_file):
                with open(hpage_file, 'r') as f:
                    size_bytes = int(f.read().strip())
                    return size_bytes // 1024  # Convert to KB
        except (IOError, OSError, ValueError):
            pass

        # Fallback: check /proc/meminfo for Hugepagesize
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('Hugepagesize:'):
                        size_kb = int(line.split()[1])
                        return size_kb
        except (IOError, OSError, ValueError):
            pass

        # Default fallback - THP is typically 2MB on x86_64
        return 2048

    def format_hugepage_size(self, size_kb: int) -> str:
        """Format hugepage size for display"""
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

    def extract_thp_info(self, smaps_files: List[str]) -> Dict[str, int]:
        """Extract transparent hugepage information from smaps files, grouped by PID"""
        pid_totals = defaultdict(int)

        for smaps_file in smaps_files:
            try:
                with open(smaps_file, 'r') as f:
                    content = f.read()

                # Extract PID from path: /proc/1234/smaps -> 1234
                pid_match = re.search(r'/proc/(\d+)/smaps', smaps_file)
                if not pid_match:
                    continue
                pid = pid_match.group(1)

                # Sum all AnonHugePages for this PID
                total_kb = 0
                for line in content.split('\n'):
                    if 'AnonHugePages:' in line and not line.endswith(' 0 kB'):
                        # Extract size from "AnonHugePages:    2048 kB"
                        anon_match = re.search(r'AnonHugePages:\s+(\d+)\s+kB', line)
                        if anon_match:
                            size_kb = int(anon_match.group(1))
                            if size_kb > 0:
                                total_kb += size_kb

                if total_kb > 0:
                    pid_totals[pid] = total_kb
                    self.debug_print(f"PID {pid}: {total_kb} kB total THP")

            except (IOError, OSError):
                continue

        self.debug_print(f"Found {len(pid_totals)} processes using transparent hugepages")
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

    def format_process_info(self, pid: str, process_name: str, total_kb: int) -> Tuple[str, str, str]:
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

        # Format the size nicely
        formatted_size = self.format_size_display(total_kb)

        return display_name, f"({pid})", formatted_size

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

    def run(self):
        """Main execution function"""
        self.check_root_privileges()

        self.debug_print(f"Starting transparent hugepage analysis (THP size: {self.hugepage_size_display})")

        # Find processes using transparent hugepages
        smaps_files = self.find_transparent_hugepage_processes()

        if not smaps_files:
            print("No Transparent HugePages found!")
            return

        # Extract transparent hugepage information (grouped by PID)
        pid_totals = self.extract_thp_info(smaps_files)

        if not pid_totals:
            print("No Transparent HugePages found!")
            return

        # Print header with hugepage size information
        print(self.print_pattern % (f"# [procname] ({self.hugepage_size_display} Transparent HugePages)", "PID", "Total THP", "Usage"))

        # Process and display results
        results = []
        grand_total_kb = 0

        for pid_str, total_kb in pid_totals.items():
            # Check if process still exists
            if not os.path.exists(f'/proc/{pid_str}/status'):
                continue

            process_name = self.get_process_name(pid_str)
            if not process_name:
                continue

            display_name, pid_display, formatted_size = self.format_process_info(pid_str, process_name, total_kb)

            # Calculate number of hugepages used by this process
            num_hugepages = total_kb // self.hugepage_size_kb
            hugepage_display = f"{num_hugepages} hugepages"

            result_line = self.print_pattern % (display_name, pid_display, formatted_size, hugepage_display)
            results.append((total_kb, result_line))  # Store with size for sorting
            grand_total_kb += total_kb

        # Sort by total KB (descending)
        results.sort(key=lambda x: x[0], reverse=True)

        # Display results
        for _, result_line in results:
            print(result_line)

        # Display grand total
        if results:
            print("-" * 60)
            grand_total_display = self.format_size_display(grand_total_kb)
            grand_total_pages = grand_total_kb // self.hugepage_size_kb
            print(self.print_pattern % ("TOTAL", "", grand_total_display, f"{grand_total_pages} hugepages"))

        self.debug_print(f"Transparent hugepage analysis completed: {len(results)} processes, {grand_total_display} total")

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
