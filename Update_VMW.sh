#!/bin/bash
FCX="fedoralib"
VTEMP_DIR=$(mktemp -p /tmp -d VMWFedora232425XXXXXXX)
MODS_SRC_DIR=/usr/lib/vmware/modules/source

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
		echo "(II) /etc/vmware/bootstrap already has VMWARE_USE_SHIPPED_LIBS, skipping.."
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

# Patch the sources..
if [ "x${VTEMP_DIR}" != "x" ]; then
	cd ${VTEMP_DIR} || exit 127
	for mymod in vmmon vmnet vmblock
	do
		if [ -f ${MODS_SRC_DIR}/${mymod}.tar ]; then
			echo "(II) Extracting  ${MODS_SRC_DIR}/${mymod}.tar  into ${VTEMP_DIR}..."
			/usr/bin/tar xf ${MODS_SRC_DIR}/${mymod}.tar  || exit 127
			for myfile in  ./vmmon-only/linux/hostif.c ./vmnet-only/userif.c
			do
				if [ -f ${myfile} ]; then
					grep -q get_user_pages_remote ${myfile}
					if [ $? -eq 0 ]; then
						echo "(II) ${myfile} from ${MODS_SRC_DIR}/${mymod}.tar already patched, skipping.."
					else
						echo "(II) Patching ${myfile} from ${MODS_SRC_DIR}/${mymod}.tar ..."
						/bin/cp -Lfv ${myfile}{,.orig}
						#perl -pi -e 's/get_user_pages/get_user_pages_remote/g' ${myfile} || exit 127
						echo "(II) Rebuilding ${MODS_SRC_DIR}/${mymod}.tar from ${VTEMP_DIR}/${mymod}-only ..."
						echo /usr/bin/tar cf ${MODS_SRC_DIR}/${mymod}.tar ${mymod}-only || exit 127
					fi
				fi
			done
		fi
	done
fi

# End
echo "(II) All done successfully. Enjoy!"
