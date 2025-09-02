#!/usr/bin/env python3
"""
VXFS Snapshot Recycling Test Script

This script creates a VXFS snapshot, mounts it briefly, then cleans it up.
It's useful for testing VXFS snapshot functionality.

Author: Converted from bash script
"""

# $Id: vxfs_recycle_snapshot.py,v 1.00 2025/09/02 17:00:00 python-conversion Exp $
__version__ = "vxfs_recycle_snapshot.py,v 1.00 2025/09/02 17:00:00 python-conversion Exp"

#
# VERSION HISTORY:
# ================
#
# v1.00 (2025-09-02): Initial Python conversion from bash script
#   - Complete Python rewrite of vxfs_recycle_snapshot.sh
#   - Enhanced debug mode: dry-run functionality with command tracing
#   - Centralized configuration section for easy customization
#   - Root privilege enforcement and proper error handling
#   - Maintains bash-like recovery behavior (no error handling by design)
#   - Clean command output formatting with # prefixes for clarity
#

import argparse
import os
import sys
import subprocess
import shutil
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='(%(levelname)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION SECTION - Modify these values as needed
# =============================================================================

# VXFS Configuration
VXFS_MOUNT_POINT = "/shared/kvm0"
VXSNAP_PREFIX = "/run/user/0"  # Always root user
VXSNAP_OPTIONS = "cachesize=1536g/autogrow=yes"
SYNC_WAIT_TIME = 5  # seconds to wait after sync before unmount

# PATH Configuration - VXFS/VCS tools locations
VXFS_PATHS = [
    '/usr/sbin',
    '/usr/bin', 
    '/opt/VRTSvcs/bin',
    '/usr/lib/vxvm/bin',
    '/opt/VRTSvxfs/sbin',
    '/opt/VRTS/bin'
]

# =============================================================================
# END CONFIGURATION SECTION
# =============================================================================


class VXFSSnapshotRecycler:
    """Class to handle VXFS snapshot recycling operations."""
    
    def __init__(self, debug=False):
        self.debug = debug
        
        # Use configuration constants
        self.vxsnap_prefix = VXSNAP_PREFIX
        self.vxsnap_opts = VXSNAP_OPTIONS
        self.kvm_fs_mnt = VXFS_MOUNT_POINT
        self.sync_wait_time = SYNC_WAIT_TIME
        
        # Set up PATH using configuration
        current_path = os.environ.get('PATH', '')
        new_path = ':'.join(VXFS_PATHS + [current_path])
        os.environ['PATH'] = new_path

    def run_command(self, command, capture_output=True):
        """Run a command without error handling - let failures happen naturally."""
        # Show command with clean prefix
        print(f"# {' '.join(command)}")
        
        if self.debug:
            # In debug mode, just show the command but don't execute (dry-run)
            from unittest.mock import Mock
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result
        
        # Always use check=False - no error handling like the bash script
        if capture_output:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False
            )
        else:
            result = subprocess.run(
                command,
                stdout=None,
                stderr=None,
                check=False
            )
        return result

    def get_vxfs_info(self):
        """Get VXFS disk group and logical volume information."""
        try:
            # Get source device
            result = self.run_command(['findmnt', '-n', '-o', 'SOURCE', self.kvm_fs_mnt])
            source = result.stdout.strip()
            parts = source.split('/')
            
            if len(parts) < 6:
                raise ValueError(f"Unexpected source format: {source}")
            
            vxlv = parts[5]  # Last part is the logical volume
            
            # Get disk group using vxprint
            result = self.run_command(['vxprint', '-r', vxlv])
            for line in result.stdout.split('\n'):
                if line.startswith('Disk group:'):
                    vxdg = line.split()[2]
                    break
            else:
                raise ValueError(f"Could not find disk group for volume {vxlv}")
            
            return vxdg, vxlv
            
        except Exception as e:
            logger.error(f"Failed to get VXFS info: {e}")
            raise

    def create_snapshot(self, vxdg, vxlv, vxsnap_lv):
        """Create VXFS snapshot - no error handling like bash script."""
        # Prepare volume - let it fail naturally
        self.run_command(['vxsnap', '-g', vxdg, 'prepare', vxlv], capture_output=False)
        
        # Create snapshot - let it fail naturally  
        self.run_command([
            'vxsnap', '-g', vxdg, 'make',
            f'source={vxlv}/newvol={vxsnap_lv}/{self.vxsnap_opts}'
        ], capture_output=False)

    def mount_snapshot(self, vxdg, vxsnap_lv, vxsnap_mnt):
        """Mount the VXFS snapshot - no error handling like bash script."""
        # Create mount directory if needed
        if not self.debug and not os.path.exists(vxsnap_mnt):
            print(f"# Creating directory {vxsnap_mnt}")
            os.makedirs(vxsnap_mnt, exist_ok=True)
        
        # Check if already mounted
        result = self.run_command(['findmnt', '-o', 'FSTYPE', vxsnap_mnt])
        
        # Check result
        fs_type = ""
        if not self.debug and result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                fs_type = lines[1].strip()
                if fs_type == 'vxfs':
                    print(f"# {vxsnap_mnt} already mounted as vxfs")
        
        # Always try to mount - bash script doesn't have early return
        self.run_command([
            'mount', '-t', 'vxfs', '-o', 'ro,noatime,largefiles',
            f'/dev/vx/dsk/{vxdg}/{vxsnap_lv}', vxsnap_mnt
        ], capture_output=False)

    def cleanup_snapshot(self, vxdg, vxlv, vxsnap_lv, vxsnap_mnt):
        """Clean up the VXFS snapshot - no error handling like bash script."""        
        # Sync and wait - let it fail naturally
        self.run_command(['sync'], capture_output=False)
        
        # Sleep for filesystem sync
        print(f"# Waiting {self.sync_wait_time} seconds for filesystem sync")
        if not self.debug:
            time.sleep(self.sync_wait_time)
        
        # Unmount - let it fail naturally
        self.run_command(['umount', vxsnap_mnt], capture_output=False)
        
        # Destroy snapshot - let it fail naturally
        self.run_command(['vxsnap', '-g', vxdg, 'dis', vxsnap_lv], capture_output=False)
        
        # Remove snapshot volume - let it fail naturally
        self.run_command(['vxedit', '-g', vxdg, '-fr', 'rm', vxsnap_lv], capture_output=False)
        
        # Unprepare volume - let it fail naturally
        self.run_command(['vxsnap', '-g', vxdg, 'unprepare', vxlv], capture_output=False)

    def run_test(self):
        """Run the complete snapshot test cycle - no error handling like bash script."""
        # Check if running as root
        if os.getuid() != 0:
            logger.error("This script must be run as root")
            logger.error("Please run: sudo " + " ".join(sys.argv))
            return 1
        
        # Get VXFS information
        try:
            if not self.debug:
                result = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', self.kvm_fs_mnt], 
                                      capture_output=True, text=True)
                source = result.stdout.strip()
                vxdg = source.split('/')[4]
                vxlv = source.split('/')[5]
            else:
                # Mock values for debug mode
                vxdg = "nvm01dg"
                vxlv = "kvm0_lv"
            
            vxsnap_lv = f"{vxlv}_snapshot"
            vxsnap_mnt = f"{self.vxsnap_prefix}/{vxsnap_lv}"
            
            print(f"# VXFS Configuration:")
            print(f"#   Disk Group: {vxdg}")
            print(f"#   Logical Volume: {vxlv}")
            print(f"#   Snapshot Volume: {vxsnap_lv}")
            print(f"#   Mount Point: {vxsnap_mnt}")
            
        except Exception as e:
            logger.error(f"Failed to get VXFS info: {e}")
            return 1
        
        # Run all commands like bash script - no error handling
        self.create_snapshot(vxdg, vxlv, vxsnap_lv)
        self.mount_snapshot(vxdg, vxsnap_lv, vxsnap_mnt)
        self.cleanup_snapshot(vxdg, vxlv, vxsnap_lv, vxsnap_mnt)
        
        return 0

    def main(self):
        """Main execution function."""
        parser = argparse.ArgumentParser(
            description='Test VXFS snapshot creation and cleanup',
            formatter_class=argparse.RawDescriptionHelpFormatter
        )
        
        parser.add_argument('-d', '--debug', action='store_true',
                           help='Debug mode - show commands without executing (dry-run)')
        parser.add_argument('-V', '--version', action='version',
                           version=__version__,
                           help='Show version information and exit')
        
        args = parser.parse_args()
        self.debug = args.debug
        
        return self.run_test()


if __name__ == '__main__':
    try:
        recycler = VXFSSnapshotRecycler()
        sys.exit(recycler.main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
