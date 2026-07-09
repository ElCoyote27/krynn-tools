#!/bin/bash
# meminfo-gap.sh - Show unaccounted kernel memory from /proc/meminfo
#
# On Dell R660 with Intel E810 (ice driver), expect ~7.9 GiB gap.
# On Dell M640/R640 with Intel 82599 (ixgbe driver), expect ~0.5 GiB.
#
# Usage:
#   ./meminfo-gap.sh                  # read local /proc/meminfo
#   ./meminfo-gap.sh /path/to/file    # read from saved meminfo file
#
# Formula:
#   Unaccounted = (MemTotal - Hugetlb)
#               - MemFree - Active(anon) - Inactive(anon)
#               - Active(file) - Inactive(file) - Unevictable
#               - Slab - KernelStack - PageTables
#               - VmallocUsed - Percpu

INPUT="${1:-/proc/meminfo}"

if [ ! -r "$INPUT" ]; then
    echo "Error: cannot read $INPUT" >&2
    exit 1
fi

awk '
  /^MemTotal:/       {mt=$2}
  /^Hugetlb:/        {ht=$2}
  /^MemFree:/        {mf=$2}
  /^Active\(anon\):/   {aa=$2}
  /^Inactive\(anon\):/ {ia=$2}
  /^Active\(file\):/   {af=$2}
  /^Inactive\(file\):/ {ifl=$2}
  /^Unevictable:/    {un=$2}
  /^Slab:/           {sl=$2}
  /^KernelStack:/    {ks=$2}
  /^PageTables:/     {pt=$2}
  /^VmallocUsed:/    {vm=$2}
  /^Percpu:/         {pc=$2}
  END {
    nonhp = mt - ht
    tracked = mf+aa+ia+af+ifl+un+sl+ks+pt+vm+pc
    gap = nonhp - tracked
    printf "%-20s %14d kB  (%7.2f GiB)\n", "MemTotal:",       mt,  mt/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Hugetlb:",        ht,  ht/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Non-HP MemTotal:", nonhp, nonhp/1048576
    printf "%s\n", "----"
    printf "%-20s %14d kB  (%7.2f GiB)\n", "MemFree:",        mf,  mf/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Active(anon):",   aa,  aa/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Inactive(anon):", ia,  ia/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Active(file):",   af,  af/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Inactive(file):", ifl, ifl/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Unevictable:",    un,  un/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Slab:",           sl,  sl/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "KernelStack:",    ks,  ks/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "PageTables:",     pt,  pt/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "VmallocUsed:",    vm,  vm/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Percpu:",         pc,  pc/1048576
    printf "%-20s %14d kB  (%7.2f GiB)\n", "Sum tracked:",    tracked, tracked/1048576
    printf "%s\n", "===="
    printf "%-20s %14d kB  (%7.2f GiB)\n", "UNACCOUNTED GAP:", gap, gap/1048576
  }
' "$INPUT"
