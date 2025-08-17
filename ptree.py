#!/usr/bin/env python3
# $Id: ptree.py,v 1.0 2024/01/01 00:00:00 converted from bash Exp $

# Python rewrite of ptree - enhanced process tree display
# Shows process hierarchy starting from the top-most meaningful parent

import os
import sys
import subprocess
import argparse
import platform
from typing import Optional, List

class ProcessTreeAnalyzer:
    def __init__(self):
        self.debug = False
        self.show_pids = True
        self.show_threads = False
        self.highlight_pid = None
        self.show_full_ancestry = True  # Default to full ancestry
        self.show_children_only = False

        # Process names to exclude as "meaningful" parents (only used when not showing full ancestry)
        self.excluded_parents = {'init', 'systemd', 'screen', 'sshd', 'kernel'}

        # Detect terminal capabilities and set default graphics preference
        self.detect_terminal_capabilities()
        self.use_graphics = self.terminal_supports_graphics  # Use graphics by default if supported

    def debug_print(self, message: str):
        """Print debug message prefixed with '#' for shell parseability"""
        if self.debug:
            print(f"# DEBUG: {message}")

    def detect_terminal_capabilities(self):
        """Detect if terminal supports graphics characters"""
        term = os.environ.get('TERM', '')
        self.debug_print(f"Terminal type: {term}")

        # Enable graphics for capable terminals
        if term in ['xterm', 'xterm-256color', 'screen', 'screen-256color', 'tmux', 'tmux-256color']:
            self.terminal_supports_graphics = True
        else:
            self.terminal_supports_graphics = False

        self.debug_print(f"Graphics support: {self.terminal_supports_graphics}")

    def check_pid_exists(self, pid: int) -> bool:
        """Check if a PID exists by checking /proc/<pid>/ directory"""
        try:
            return os.path.exists(f"/proc/{pid}")
        except (OSError, ValueError):
            return False

    def get_process_info(self, pid: int) -> Optional[dict]:
        """Get process information from /proc/<pid>/status"""
        try:
            with open(f'/proc/{pid}/status', 'r') as f:
                info = {}
                for line in f:
                    if line.startswith('Name:'):
                        info['name'] = line.split()[1]
                    elif line.startswith('PPid:'):
                        info['ppid'] = int(line.split()[1])

                if 'name' in info and 'ppid' in info:
                    return info

        except (IOError, OSError, ValueError, IndexError):
            pass

        return None

    def find_actual_root(self, pid: int) -> int:
        """Walk up the process tree to find the actual root (systemd/init)"""
        self.debug_print(f"Finding actual root for PID {pid}")

        current_pid = pid
        root_parent = pid

        while current_pid > 0:
            proc_info = self.get_process_info(current_pid)
            if not proc_info:
                self.debug_print(f"Could not get info for PID {current_pid}")
                break

            ppid = proc_info['ppid']
            parent_name = None

            # Get parent process name
            if ppid > 1:
                parent_info = self.get_process_info(ppid)
                if parent_info:
                    parent_name = parent_info['name']

            self.debug_print(f"PID {current_pid} ({proc_info['name']}) -> PPID {ppid} ({parent_name})")

            # Stop only at init (PID 1) or when we can't go further
            if ppid <= 1:
                self.debug_print(f"Reached root: {parent_name} (PID {ppid})")
                break

            # Move up the tree
            root_parent = ppid
            current_pid = ppid

        self.debug_print(f"Actual root parent: {root_parent}")
        return root_parent

    def find_top_parent(self, pid: int) -> int:
        """Walk up the process tree to find the appropriate root"""
        # If showing children only, start from the target PID
        if self.show_children_only:
            self.debug_print(f"Showing children only for PID {pid}")
            return pid

        # If showing full ancestry, use actual root  
        if self.show_full_ancestry:
            return self.find_actual_root(pid)

        self.debug_print(f"Finding top meaningful parent for PID {pid}")

        current_pid = pid
        top_parent = pid

        while current_pid > 0:
            proc_info = self.get_process_info(current_pid)
            if not proc_info:
                self.debug_print(f"Could not get info for PID {current_pid}")
                break

            ppid = proc_info['ppid']
            parent_name = None

            # Get parent process name
            if ppid > 1:
                parent_info = self.get_process_info(ppid)
                if parent_info:
                    parent_name = parent_info['name']

            self.debug_print(f"PID {current_pid} ({proc_info['name']}) -> PPID {ppid} ({parent_name})")

            # Stop if parent is init, systemd, or other excluded processes
            if ppid <= 1 or not parent_name or parent_name in self.excluded_parents:
                self.debug_print(f"Stopping at parent {parent_name} (PID {ppid})")
                break

            # Move up the tree
            top_parent = ppid
            current_pid = ppid

        self.debug_print(f"Top parent found: PID {top_parent}")
        return top_parent

    def build_pstree_command(self, root_pid: int) -> List[str]:
        """Build the pstree command with appropriate options"""
        cmd = ['pstree']

        # Build format string to match original shell script behavior
        format_args = ""

        # Character set options
        if self.use_graphics and self.terminal_supports_graphics:
            format_args += "G"  # Use VT100 line drawing characters
            self.debug_print("Using graphics characters")
        else:
            format_args += "A"  # Use ASCII characters
            self.debug_print("Using ASCII characters")

        # Display options (matching original shell script -planA)
        if self.show_pids:
            format_args += "p"  # Show PIDs

        format_args += "l"     # Long format (don't truncate)
        format_args += "a"     # Show command line arguments
        format_args += "n"     # Sort processes numerically by PID

        # Add combined format argument
        cmd.append(f"-{format_args}")

        # Show threads if requested (separate argument)
        if self.show_threads:
            cmd.append('-t')

        # Highlight specific PID if requested (separate argument)
        if self.highlight_pid:
            cmd.extend(['-H', str(self.highlight_pid)])

        # Root PID
        cmd.append(str(root_pid))

        self.debug_print(f"pstree command: {' '.join(cmd)}")
        return cmd

    def run_pstree(self, root_pid: int) -> bool:
        """Execute pstree with the specified root PID"""
        try:
            cmd = self.build_pstree_command(root_pid)

            # Execute pstree
            result = subprocess.run(cmd, check=False, text=True)

            if result.returncode != 0:
                print(f"pstree exited with code {result.returncode}", file=sys.stderr)
                return False

            return True

        except FileNotFoundError:
            print("Error: pstree command not found. Please install psmisc package.", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Error executing pstree: {e}", file=sys.stderr)
            return False

    def run(self, pid: int) -> int:
        """Main execution function"""
        self.debug_print(f"Starting process tree analysis for PID {pid}")

        # Sanity check: Linux only
        if platform.system() != 'Linux':
            print(f"Error: Not supported on {platform.system()}!", file=sys.stderr)
            return 1

        # Check if PID exists
        if not self.check_pid_exists(pid):
            print(f"Error: PID {pid} does not exist!", file=sys.stderr)
            return 1

        # Find the top-most meaningful parent
        try:
            top_parent = self.find_top_parent(pid)
        except Exception as e:
            if self.debug:
                raise
            print(f"Error finding parent process: {e}", file=sys.stderr)
            return 1

        # Execute pstree (don't auto-highlight unless user requested it)
        success = self.run_pstree(top_parent)

        self.debug_print("Process tree analysis completed")
        return 0 if success else 1

def main():
    parser = argparse.ArgumentParser(
        description="Enhanced process tree display - shows complete process ancestry by default",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 1234              # Show complete ancestry for PID 1234 (default, auto-detects graphics)
  %(prog)s 1234 --children   # Show only PID 1234 and its children (no parent ancestry)
  %(prog)s 1234 --meaningful # Show meaningful parent tree (stops at systemd/init)
  %(prog)s 1234 --no-graphics # Force ASCII characters instead of graphics
  %(prog)s 1234 --graphics   # Force graphics even if terminal detection fails
  %(prog)s 1234 --highlight 1234 # Highlight PID 1234 in the tree
  %(prog)s 1234 --no-pids    # Hide PIDs in output
  %(prog)s 1234 --threads    # Show threads as well as processes
  %(prog)s 1234 --debug      # Show debug information including terminal detection

Note: Graphics characters are used automatically when terminal supports them.
By default, this tool shows complete process ancestry up to systemd/init.
Use --children to show only the target process and descendants.
Use --meaningful to show only the meaningful parent tree (old default behavior).
        """
    )

    parser.add_argument('pid', type=int,
                       help='Process ID to analyze')

    parser.add_argument('--graphics', '-g', action='store_true',
                       help='Force graphics characters for tree display (even if terminal detection fails)')

    parser.add_argument('--no-graphics', action='store_true',
                       help='Force ASCII characters instead of graphics (override auto-detection)')

    parser.add_argument('--children', '-c', action='store_true',
                       help='Show only target PID and its children (no parent ancestry)')

    parser.add_argument('--meaningful', '-m', action='store_true',
                       help='Show only meaningful parent tree (stops at systemd/init, old default behavior)')

    parser.add_argument('--no-pids', action='store_true',
                       help='Hide process IDs in the output')

    parser.add_argument('--threads', '-t', action='store_true',
                       help='Show threads in addition to processes')

    parser.add_argument('--highlight', type=int, metavar='PID',
                       help='Highlight a specific PID in the tree')

    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output showing analysis details (prefixed with #)')

    args = parser.parse_args()

    # Create and configure analyzer
    analyzer = ProcessTreeAnalyzer()
    analyzer.debug = args.debug
    analyzer.show_pids = not args.no_pids
    analyzer.show_threads = args.threads

    # Handle tree display modes (mutually exclusive)
    if args.children:
        analyzer.show_children_only = True
        analyzer.show_full_ancestry = False
    elif args.meaningful:
        analyzer.show_children_only = False
        analyzer.show_full_ancestry = False
    # else: keep default (show_full_ancestry = True)

    # Handle graphics options (priority: --no-graphics > --graphics > auto-detect)
    if args.no_graphics:
        analyzer.use_graphics = False
        analyzer.debug_print("Graphics mode: forced ASCII (--no-graphics)")
    elif args.graphics:
        analyzer.use_graphics = True
        analyzer.debug_print("Graphics mode: forced graphics (--graphics)")
    else:
        # Use auto-detected value from __init__
        analyzer.debug_print(f"Graphics mode: auto-detected ({'graphics' if analyzer.use_graphics else 'ASCII'})")

    if args.highlight:
        analyzer.highlight_pid = args.highlight

    # Run analysis
    try:
        return analyzer.run(args.pid)
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
