#!/usr/bin/env python3
# $Id: lsfd.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of lsfd - File descriptor usage checker
# Cross-platform script for monitoring FD usage against system limits

import os
import sys
import re
import pwd
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict

class FileDescriptorAnalyzer:
    def __init__(self):
        self.debug = False
        self.version = "0.6-py"

        # Configuration
        self.threshold = 30  # Default threshold percentage
        self.detailed_mode = False
        self.return_mode = False  
        self.max_usage_mode = False
        self.quiet_mode = False
        self.target_user = None

        # System info
        self.current_uid = os.getuid()
        self.current_user = pwd.getpwuid(self.current_uid).pw_name
        self.platform = os.uname().sysname

        # Process caches
        self.process_cache = {}
        self.fd_cache = {}

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def get_uid_from_user(self, username: str) -> Optional[int]:
        """Get UID from username"""
        try:
            return pwd.getpwnam(username).pw_uid
        except KeyError:
            return None

    def get_user_from_uid(self, uid: int) -> Optional[str]:
        """Get username from UID"""
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None

    def get_current_users(self) -> Set[str]:
        """Get list of users with active processes"""
        users = set()
        try:
            proc_dirs = [d for d in os.listdir('/proc') if d.isdigit()]
            for pid in proc_dirs:
                try:
                    stat = os.stat(f'/proc/{pid}')
                    username = self.get_user_from_uid(stat.st_uid)
                    if username:
                        users.add(username)
                except (OSError, FileNotFoundError):
                    continue
        except OSError:
            pass

        self.debug_print(f"Found active users: {sorted(users)}")
        return users

    def get_user_processes(self, username: str) -> List[int]:
        """Get list of process PIDs for a specific user"""
        processes = []
        target_uid = self.get_uid_from_user(username)
        if target_uid is None:
            return processes

        self.debug_print(f"Looking for processes owned by {username} (UID: {target_uid})")

        try:
            proc_dirs = [d for d in os.listdir('/proc') if d.isdigit()]
            for pid_str in proc_dirs:
                try:
                    pid = int(pid_str)
                    stat = os.stat(f'/proc/{pid}')
                    if stat.st_uid == target_uid:
                        processes.append(pid)
                except (OSError, ValueError, FileNotFoundError):
                    continue
        except OSError:
            pass

        self.debug_print(f"Found {len(processes)} processes for user {username}")
        return sorted(processes)

    def count_file_descriptors(self, pid: int) -> int:
        """Count open file descriptors for a process"""
        if pid in self.fd_cache:
            return self.fd_cache[pid]

        try:
            fd_dir = f'/proc/{pid}/fd'
            fd_count = len([f for f in os.listdir(fd_dir) if f.isdigit()])
            self.fd_cache[pid] = fd_count
            return fd_count
        except (OSError, FileNotFoundError):
            return 0

    def get_process_limits(self, pid: int) -> Tuple[int, int]:
        """Get process file descriptor limits (soft, hard)"""
        try:
            with open(f'/proc/{pid}/limits', 'r') as f:
                for line in f:
                    if 'Max open files' in line or 'open files' in line:
                        parts = line.split()
                        if len(parts) >= 3:
                            try:
                                soft = int(parts[3]) if parts[3] != 'unlimited' else 2147483647
                                hard = int(parts[4]) if parts[4] != 'unlimited' else 2147483647
                                return soft, hard
                            except (ValueError, IndexError):
                                continue
        except (OSError, FileNotFoundError):
            pass

        # Default limits if unable to read
        return 1024, 4096

    def get_process_command(self, pid: int) -> str:
        """Get process command line"""
        if pid in self.process_cache:
            return self.process_cache[pid]

        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().decode('utf-8', errors='ignore')
                # Replace null bytes with spaces and clean up
                cmdline = ' '.join(cmdline.split('\0')).strip()
                if not cmdline:
                    # Fallback to comm if cmdline is empty
                    with open(f'/proc/{pid}/comm', 'r') as comm_f:
                        cmdline = f"[{comm_f.read().strip()}]"
        except (OSError, FileNotFoundError):
            cmdline = f"<process {pid}>"

        self.process_cache[pid] = cmdline
        return cmdline

    def analyze_process_fd_usage(self, processes: List[int]) -> List[Dict]:
        """Analyze file descriptor usage for a list of processes"""
        results = []

        self.debug_print(f"Analyzing FD usage for {len(processes)} processes")

        for pid in processes:
            try:
                # Check if process still exists
                if not os.path.exists(f'/proc/{pid}'):
                    continue

                fd_count = self.count_file_descriptors(pid)
                soft_limit, hard_limit = self.get_process_limits(pid)

                # Calculate percentage based on soft limit
                if soft_limit > 0:
                    percentage = int((fd_count / soft_limit) * 100)
                else:
                    percentage = 0

                command = self.get_process_command(pid)

                results.append({
                    'pid': pid,
                    'fd_count': fd_count,
                    'soft_limit': soft_limit,
                    'hard_limit': hard_limit,
                    'percentage': percentage,
                    'command': command
                })

                self.debug_print(f"PID {pid}: {fd_count}/{soft_limit} FDs ({percentage}%)")

            except (OSError, FileNotFoundError):
                # Process disappeared
                continue

        return results

    def filter_results(self, results: List[Dict]) -> List[Dict]:
        """Filter results based on mode and threshold"""
        if self.max_usage_mode:
            # Return only the process with maximum usage
            if results:
                max_result = max(results, key=lambda x: (x['percentage'], x['fd_count']))
                self.debug_print(f"Max usage process: PID {max_result['pid']} ({max_result['percentage']}%)")
                return [max_result]
            return []

        elif self.detailed_mode:
            # Return all processes
            self.debug_print(f"Detailed mode: returning all {len(results)} processes")
            return results

        else:
            # Return processes above threshold
            filtered = [r for r in results if r['percentage'] >= self.threshold]
            self.debug_print(f"Threshold filter ({self.threshold}%): {len(filtered)}/{len(results)} processes")
            return filtered

    def format_and_display_results(self, results: List[Dict]):
        """Format and display the results"""
        if not results:
            if not self.quiet_mode:
                if self.max_usage_mode:
                    print("No processes found.")
                else:
                    print(f"No process has a FD usage higher than {self.threshold}%")
            return len(results)

        # Print header
        if not self.quiet_mode:
            print("  PID   USED   SOFT   HARD  PCTUSED  PROCESS")

        # Sort results by PID
        sorted_results = sorted(results, key=lambda x: x['pid'])

        # Display results
        for result in sorted_results:
            # Handle unlimited values display
            soft_display = result['soft_limit'] if result['soft_limit'] < 2147483647 else 'unlim'
            hard_display = result['hard_limit'] if result['hard_limit'] < 2147483647 else 'unlim'

            print(f"{result['pid']:5d} {result['fd_count']:6d} {soft_display:>6} {hard_display:>6} {result['percentage']:3d}% {result['command']}")

        return len(results)

    def run_analysis(self) -> int:
        """Run the file descriptor analysis"""
        self.debug_print(f"Starting FD analysis for user: {self.target_user}")
        self.debug_print(f"Platform: {self.platform}, Current user: {self.current_user}")

        # Get processes for target user
        processes = self.get_user_processes(self.target_user)

        if not processes:
            if not self.quiet_mode:
                print(f"No processes found for user {self.target_user}")
            return 0

        # Analyze FD usage
        results = self.analyze_process_fd_usage(processes)

        # Filter results based on mode
        filtered_results = self.filter_results(results)

        # Display results
        return self.format_and_display_results(filtered_results)

    def validate_configuration(self) -> List[str]:
        """Validate configuration and return list of errors"""
        errors = []

        # Check if monitoring another user without root privileges
        if (self.target_user != self.current_user and 
            self.current_user != 'root'):
            errors.append(f"Cannot monitor another user process without being root "
                         f"(current: {self.current_user}, requested: {self.target_user})")

        # Check incompatible mode combinations
        if self.return_mode and self.detailed_mode:
            errors.append("Return mode and Detailed mode at the same time doesn't make sense")

        if (self.max_usage_mode and 
            (self.detailed_mode or self.return_mode)):
            errors.append('Mode max usage is exclusive with modes "return" and "detailed"')

        # Validate threshold range
        if not (0 <= self.threshold <= 100):
            errors.append("Threshold must be in range 0-100")

        # Check if target user exists
        if self.get_uid_from_user(self.target_user) is None:
            errors.append(f"Cannot get username for {self.target_user}")

        return errors

def main():
    parser = argparse.ArgumentParser(
        description=f"Unix File descriptor usage checker v{FileDescriptorAnalyzer().version}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Check FD usage for current user
  %(prog)s -u root            # Check FD usage for root user  
  %(prog)s -t 50 -d           # Detailed view with 50%% threshold
  %(prog)s -m                 # Show only max usage process
  %(prog)s --debug            # Show with debug information
        """
    )

    parser.add_argument('-t', '--threshold', type=int, default=30, metavar='PCT',
                       help='Threshold value over which the process is marked as high FD usage (default: 30)')

    parser.add_argument('-d', '--detailed', action='store_true',
                       help='Use detailed display mode (show all processes)')

    parser.add_argument('-u', '--user', metavar='USER',
                       help='Display the user FD usage (default: current user)')

    parser.add_argument('-r', '--return-mode', action='store_true',
                       help='Return mode - exit with count of processes above threshold')

    parser.add_argument('-m', '--max-usage', action='store_true',
                       help='Max usage mode - display only the process with maximum percentage usage')

    parser.add_argument('-q', '--quiet', action='store_true',
                       help="Quiet mode - don't display headers or information, only process list")

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = FileDescriptorAnalyzer()
    analyzer.debug = args.debug
    analyzer.threshold = args.threshold
    analyzer.detailed_mode = args.detailed
    analyzer.return_mode = args.return_mode
    analyzer.max_usage_mode = args.max_usage
    analyzer.quiet_mode = args.quiet

    # Set target user (default to current user)
    if args.user:
        analyzer.target_user = args.user
    else:
        analyzer.target_user = analyzer.current_user

    # Validate configuration
    errors = analyzer.validate_configuration()
    if errors:
        print("Errors occurred:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        sys.exit(1)

    # Run analysis
    try:
        result_count = analyzer.run_analysis()

        # Return mode: exit with count
        if analyzer.return_mode:
            sys.exit(result_count)
        else:
            sys.exit(0)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        if analyzer.debug:
            raise
        else:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
