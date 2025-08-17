#!/bin/bash
set -ex

QCOW2_OPTIONS="preallocation=metadata,cluster_size=64k,lazy_refcounts=on"

if [[ -z "$1" ]]; then
	echo "Usage: $0 vm1-*.qcow2"
	exit 0
fi

# Main loop
for mydisk in $*
do
	/bin/mv -fv ${mydisk} ${mydisk}.orig
	qemu-img convert -p -O qcow2 -o ${QCOW2_OPTIONS} ${mydisk}.orig ${mydisk} && /bin/rm -fv ${mydisk}.orig || exit 127
done
