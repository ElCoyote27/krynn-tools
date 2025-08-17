#!/bin/bash
# $Id: lsthp,v 1.5 2019/04/03 21:17:33 root Exp $
#
PRINT_PATTERN="%-24s %-10s : %s %s\n"
typeset -i index=0

if [[ $UID -ne 0 ]]; then
	echo "Run $0 as root!" ; exit 127
fi

# Loop

huge_pid_list=$(/bin/egrep -H -s 'AnonHugePages:' /proc/*/smaps|grep -v ' 0 kB$'|cut -d: -f1|xargs)
if [[ -z ${huge_pid_list} ]]; then
	printf "No Transparent HugePages found!\n"
else
	if [[ $index -eq 0 ]]; then
		# Header
		printf "${PRINT_PATTERN}" "# [procname]" "PID" "Size" "unit"
	fi

	(/bin/grep -H -s AnonHugePages ${huge_pid_list}| \
		grep -v '0 kB$'| \
		sed -e 's@/proc/@@' -e 's@/smaps:AnonHugePages:@@' | \
		awk '{ print $2,$3,$1}'| \
		sort -n | \
		while read size unit pid
	do
		if [[ -f /proc/${pid}/status ]]; then
			pidname=$(grep -s 'Name:' /proc/${pid}/status|awk '{ print $2}')
			if [[  "$pidname" = "qemu-kvm" ]]; then
				guestname="$(strings -a /proc/${pid}/cmdline |grep -A1 -i '^-name'|tail -1)"
				printf "${PRINT_PATTERN}" "${pidname} [ ${guestname} ]" "(${pid})" "${size}" "${unit}"
			else
				printf "${PRINT_PATTERN}" "${pidname}" "(${pid})" "${size}" "${unit}"
			fi
		fi
		index=1
	done)|sort -u
fi

