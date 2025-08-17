#!/usr/bin/env python3
# $Id: lskfds.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of Find_Deleted_Inodes.sh - finds processes holding killed file descriptors  
# Renamed to 'lskfds' (list killed file descriptors) for technical accuracy and consistency

import os
import sys
import re
import glob
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

class KilledFileDescriptorsAnalyzer:
    def __init__(self):
        self.debug = False
        self.show_size = False
        self.sort_by_size = False
        self.min_size_mb = 0

        # Output formatting
        self.header_format = "{:<8} {:<8} {:<22} {:<15} {}"
        self.row_format = "{:<8} {:<8} {:<22} {:<15} {}"

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def get_file_size(self, fd_path: str) -> int:
        """Get file size from file descriptor, return size in bytes"""
        try:
            stat_info = os.stat(fd_path)
            return stat_info.st_size
        except (OSError, IOError):
            return 0

    def get_process_info(self, pid: str) -> Tuple[Optional[str], Optional[str]]:
        """Get process command and full command line"""
        try:
            # Get command name from /proc/<pid>/comm (more reliable than ps)
            with open(f'/proc/{pid}/comm', 'r') as f:
                comm = f.read().strip()

            # Get full command line for additional context
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline_bytes = f.read()
                    cmdline = cmdline_bytes.decode('utf-8', errors='ignore').replace('\0', ' ').strip()
                    # Truncate very long command lines
                    if len(cmdline) > 50:
                        cmdline = cmdline[:47] + "..."
            except (IOError, OSError):
                cmdline = comm

            return comm, cmdline

        except (IOError, OSError):
            # Fallback to ps command
            try:
                result = subprocess.run(['ps', '-p', pid, '-o', 'comm='], 
                                      capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    comm = result.stdout.strip()
                    return comm, comm
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                pass

            return None, None

    def format_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024**3):.1f}G"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024**2):.1f}M"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f}K"
        else:
            return f"{size_bytes}B"

    def scan_killed_file_descriptors(self) -> List[Tuple[str, str, str, str, str, int]]:
        """Scan all processes for killed file descriptors"""
        self.debug_print("Starting killed file descriptor scan")
        killed_fds = []

        # Get all numeric PID directories
        try:
            proc_dirs = [d for d in os.listdir('/proc') if d.isdigit()]
        except (OSError, IOError):
            self.debug_print("Cannot access /proc directory")
            return []

        self.debug_print(f"Scanning {len(proc_dirs)} processes")

        for pid in proc_dirs:
            fd_dir = f'/proc/{pid}/fd'

            # Check if fd directory is accessible
            if not os.path.isdir(fd_dir) or not os.access(fd_dir, os.R_OK):
                continue

            try:
                # List all file descriptors
                fd_entries = os.listdir(fd_dir)

                for fd_name in fd_entries:
                    fd_path = os.path.join(fd_dir, fd_name)

                    try:
                        # Resolve the symlink
                        link_target = os.readlink(fd_path)

                        # Check if this is a killed file descriptor (deleted file)
                        if '(deleted)' in link_target:
                            # Get process information
                            comm, cmdline = self.get_process_info(pid)
                            if not comm:
                                continue

                            # Get file size if requested
                            file_size = 0
                            if self.show_size:
                                file_size = self.get_file_size(fd_path)

                                # Skip files smaller than minimum size
                                if self.min_size_mb > 0 and file_size < (self.min_size_mb * 1024 * 1024):
                                    continue

                            # Clean up the killed file path
                            clean_path = link_target.replace(' (deleted)', '')

                            killed_fds.append((pid, fd_name, comm, cmdline, clean_path, file_size))
                            self.debug_print(f"Found killed FD: PID {pid}, FD {fd_name}, CMD {comm}, FILE {clean_path}")

                    except (OSError, IOError):
                        # File descriptor may have disappeared or be inaccessible
                        continue

            except (OSError, IOError):
                # Process may have disappeared or fd directory inaccessible
                continue

        self.debug_print(f"Found {len(killed_fds)} killed file descriptors")
        return killed_fds

    def display_results(self, killed_fds: List[Tuple[str, str, str, str, str, int]]):
        """Display the results in a formatted table"""
        if not killed_fds:
            print("No killed file descriptors found.")
            return

        # Sort results
        if self.sort_by_size and self.show_size:
            killed_fds.sort(key=lambda x: x[5], reverse=True)  # Sort by size descending
        else:
            killed_fds.sort(key=lambda x: (int(x[0]), int(x[1])))  # Sort by PID, then FD

        # Print header
        if self.show_size:
            print(self.header_format.format("PID", "FD", "CMD", "SIZE", "KILLED FILE"))
            print("-" * 80)
        else:
            print("{:<8} {:<8} {:<22} {}".format("PID", "FD", "CMD", "KILLED FILE"))
            print("-" * 60)

        # Print results
        total_size = 0
        for pid, fd, comm, cmdline, killed_path, file_size in killed_fds:
            if self.show_size:
                size_str = self.format_size(file_size)
                print(self.row_format.format(pid, fd, comm, size_str, killed_path))
                total_size += file_size
            else:
                print("{:<8} {:<8} {:<22} {}".format(pid, fd, comm, killed_path))

        # Show summary
        print("-" * (80 if self.show_size else 60))
        if self.show_size:
            print(f"Total: {len(killed_fds)} killed files, {self.format_size(total_size)} wasted space")
        else:
            print(f"Total: {len(killed_fds)} killed file descriptors found")

    def run(self):
        """Main execution function"""
        self.debug_print("Starting killed file descriptor analysis")

        # Scan for killed file descriptors
        killed_fds = self.scan_killed_file_descriptors()

        # Display results
        self.display_results(killed_fds)

        self.debug_print("Killed file descriptor analysis completed")

def main():
    parser = argparse.ArgumentParser(
        description="Find processes holding file descriptors to killed (deleted) files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Show all killed file descriptors
  %(prog)s --size             # Show with file sizes
  %(prog)s --size --sort      # Show with sizes, sorted by size
  %(prog)s --min-size 10      # Only show files >= 10MB
  %(prog)s --debug            # Show debug information

Note: This tool helps identify processes holding onto killed (deleted) files,
which can prevent disk space from being freed until the process is restarted.
        """
    )

    parser.add_argument('--size', '-s', action='store_true',
                       help='Show file sizes and calculate wasted disk space')

    parser.add_argument('--sort', action='store_true',
                       help='Sort by file size (requires --size)')

    parser.add_argument('--min-size', type=int, default=0, metavar='MB',
                       help='Only show files larger than specified MB (requires --size)')

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = KilledFileDescriptorsAnalyzer()
    analyzer.debug = args.debug
    analyzer.show_size = args.size or args.min_size > 0
    analyzer.sort_by_size = args.sort
    analyzer.min_size_mb = args.min_size

    # Validate arguments
    if args.sort and not analyzer.show_size:
        print("Warning: --sort requires --size, enabling size display", file=sys.stderr)
        analyzer.show_size = True

    # Run analysis
    try:
        analyzer.run()
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
