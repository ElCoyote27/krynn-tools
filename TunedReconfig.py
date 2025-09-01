#!/usr/bin/env python3
"""
TunedReconfig - Simple script to switch between tuned profiles

A utility to quickly switch between common tuned profiles:
- powersave
- virtual-host intel-sst
"""

# $Id: TunedReconfig.py 1.00 2025/01/27 00:00:00 initial-release Exp $
__version__ = "TunedReconfig.py 1.00 2025/01/27 00:00:00 initial-release Exp"

#
# VERSION HISTORY:
# ================
#
# v1.00 (2025-01-27): Initial release
#   - Simple profile switching between powersave and virtual-host intel-sst
#   - Support for short aliases (p/v) and full profile names
#   - Uses sudo for privilege escalation with tuned-adm
#   - Status display and toggle suggestions
#   - Command line argument support with help text
#

import subprocess
import sys
import argparse

PROFILES = {
    'p': 'powersave',
    'power': 'powersave',
    'powersave': 'powersave',
    'v': 'virtual-host intel-sst',
    'virtual': 'virtual-host intel-sst',
    'virtual-host': 'virtual-host intel-sst',
    'intel': 'virtual-host intel-sst'
}

def get_current_profile():
    """Get the currently active tuned profile"""
    try:
        result = subprocess.run(['tuned-adm', 'active'], 
                              capture_output=True, text=True, check=True)
        # Output format: "Current active profile: <profile>"
        if 'Current active profile:' in result.stdout:
            return result.stdout.split('Current active profile:')[1].strip()
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"
    except FileNotFoundError:
        print("Error: tuned-adm not found. Is tuned installed?")
        sys.exit(1)

def set_profile(profile, quiet=False):
    """Set the tuned profile using sudo"""
    try:
        subprocess.run(['sudo', 'tuned-adm', 'profile', profile], check=True)
        if not quiet:
            print(f"Successfully switched to profile: {profile}")
    except subprocess.CalledProcessError as e:
        print(f"Error switching to profile '{profile}': {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: sudo or tuned-adm not found")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description='Switch between tuned profiles',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available profiles:
  p, power, powersave     -> powersave
  v, virtual, intel       -> virtual-host intel-sst

Examples:
  TunedReconfig.py p      Switch to powersave
  TunedReconfig.py v      Switch to virtual-host intel-sst
  TunedReconfig.py -q p   Switch to powersave silently
  TunedReconfig.py        Show current profile and toggle options
        """)
    parser.add_argument('profile', nargs='?', 
                       help='Profile to switch to (p/v or full name)')
    parser.add_argument('-s', '--status', action='store_true',
                       help='Show current profile only')
    parser.add_argument('-q', '--quiet', action='store_true',
                       help='Suppress all output except critical errors')
    parser.add_argument('--version', action='version', version=__version__,
                       help='Show version information')

    args = parser.parse_args()

    current = get_current_profile()

    if args.status:
        if not args.quiet:
            print(f"Current profile: {current}")
        return

    if not args.profile:
        if not args.quiet:
            print(f"Current profile: {current}")
            print("\nAvailable options:")
            print("  p/power     -> powersave")
            print("  v/virtual   -> virtual-host intel-sst")

            # Auto-suggest toggle
            if current == 'powersave':
                print(f"\nSuggestion: TunedReconfig.py v  (switch to virtual-host intel-sst)")
            elif current == 'virtual-host intel-sst':
                print(f"\nSuggestion: TunedReconfig.py p  (switch to powersave)")
        return

    # Look up the profile
    profile_key = args.profile.lower()
    if profile_key not in PROFILES:
        print(f"Unknown profile: {args.profile}")
        print("Valid options: p, power, powersave, v, virtual, intel")
        sys.exit(1)

    target_profile = PROFILES[profile_key]

    if current == target_profile:
        if not args.quiet:
            print(f"Already using profile: {current}")
    else:
        if not args.quiet:
            print(f"Switching from '{current}' to '{target_profile}'")
        set_profile(target_profile, quiet=args.quiet)

if __name__ == '__main__':
    main()
