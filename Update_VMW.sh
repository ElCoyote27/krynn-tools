#!/bin/bash
FCX="fedoralib"

# Check for root
if [ "x$(id -u)" != "x0" ]; then
	echo "(**) Run this tool as root!"; exit 1
fi

# Check distro
KVER=$(uname -r|grep fc23)
if [ "x${KVER}" = "x" ];then
	echo "(**) Fedora 23 not detected, Exit!"
	exit 1
fi

# Check presence of VMW
if [ -f /usr/lib/vmware/lib/libvmwareui.so/libvmwareui.so ]; then
	echo "(II) /usr/lib/vmware/lib/libvmwareui.so/libvmwareui.so present, continuing..."
else
	echo "(**) VMWare Workstation not detected, exit!"; exit 1
fi

# Force use of VMWare bundled libs
if [ -f /etc/vmware/bootstrap ]; then
	grep -q VMWARE_USE_SHIPPED_LIBS /etc/vmware/bootstrap
	if [ $? -eq 0 ]; then
		echo "(II) /etc/vmware/bootstrap already has VMWARE_USE_SHIPPED_LIBS, skip.."
	else
		echo "(II) Patching /etc/vmware/bootstrap..."
		echo "export VMWARE_USE_SHIPPED_LIBS=force" >> /etc/vmware/bootstrap
	fi
	
fi

#

for mylib in $(rpm -ql glib2|grep '/usr/lib64/libg.*so\.0$')
do
	tgtlib="/usr/lib/vmware/lib/$(basename $mylib)/$(basename $mylib)"
	if [ ! -f "${tgtlib}.${FCX}" ]; then
		echo "(II) Backing up to ${tgtlib}.${FCX}..."
		/bin/cp -Lfv ${tgtlib} ${tgtlib}.${FCX}
	fi
	echo "(II) Replacing ${tgtlib} ..."
 	/bin/cp -Lfv ${mylib} ${tgtlib}
done
