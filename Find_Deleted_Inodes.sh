#!/bin/bash

echo "PID    FD     CMD                   DELETED FILE"
echo "--------------------------------------------------------"

# Iterate through all PIDs
for pid in $(ls /proc | grep -E '^[0-9]+$'); do
    fd_dir="/proc/$pid/fd"

    # Make sure the fd directory is accessible (e.g., not a zombie or inaccessible process)
    if [ -d "$fd_dir" ] && [ -r "$fd_dir" ]; then
        for fd in "$fd_dir"/*; do
            # Resolve the symlink
            link=$(readlink -f "$fd" 2>/dev/null)

            if [[ "$link" == *"(deleted)" ]]; then
                fd_num=$(basename "$fd")
                cmd=$(ps -p $pid -o comm=)
                printf "%-6s %-6s %-20s %s\n" "$pid" "$fd_num" "$cmd" "$link"
            fi
        done
    fi
done

