#!/bin/bash
# $Id: ptree,v 1.15 2019/07/10 20:19:21 root Exp $

# ptree only supports one and exactly one PID so forget other args..
# Initialization
myPID=$1
myPPID=${myPID}
myTOP=${myPID}

# Terminals
case $TERM in
	xterm|vt100|screen*)
		#MPSARGS="-planG"		### Graphics
		MPSARGS="-planA"		### ASCII
		;;
	*)
		MPSARGS="-planA"		### ASCII
		;;
esac

#Sub-routines
errecho() {
	>&2 echo "$*"
}

CheckPID() {
	# ptree only supports one and exactly one PID so forget about other args..
	if [ "x$1" = "x" ]; then
		errecho "no PID specified!"
		return 0
	fi

	# Verify PID's presence
	ps --no-heading -o pid ${myPID} > /dev/null 2>&1 || {
		errecho "PID $myPID does not exist!"
		return 1
	}
	return 0
}

GetPPID() {
	CheckPID $1 || exit 127
	localPPID=$(awk '{ if ( $1 ~ "PPid:" ) print $2 }' /proc/$1/status)
	[ -f /proc/${localPPID}/status ] && {
		lPidName=$(awk '{ if ( $1 ~ "Name:" ) print $2 }' /proc/${localPPID}/status)
		# localPPID=`ps --no-heading -o ppid ${myPPID} 2> /dev/null|xargs`
		[ "x${localPPID}" != "x" ] && {
			[ "x${lPidName}" != "x" -a "x${lPidName}" != "xinit" -a "x${lPidName}" != "xsystemd" -a "x${lPidName}" != "xscreen" -a "x${lPidName}" != "xsshd" ] && {
				echo ${localPPID}; return 0
			} || {
				# errecho "Discarded PPID: ${localPPID}"
				echo 0 ; return 1
			}
		} || {
			echo 0 ; return 1
		}
	} || {
		echo 0 ; return 1
	}
	return 0
}
#

# Sanity Check
if [ "x$(uname -s)" != "xLinux" ]; then
	echo "Not supported on $(uname -s)! Exit!"
	exit 127
fi

# Sanity Check (2)
CheckPID ${myPID} || exit 127

# Main routine
if [ "x${myTOP}" != "x" ]; then
	# Walk up to Find Parent until myPPID=1
	while [ ${myTOP} -gt 0 ]; do
		myTOP=$(GetPPID ${myPPID})
		if [ "x${myTOP}" != "x" ]; then
			if [ ${myTOP} -gt 1 ]; then
				myPPID=${myTOP}
				## echo "Found PPID: ${myPPID}"
			fi
		fi
	done
	#exec /usr/bin/pstree ${MPSARGS} ${myPPID}|col -b
	/usr/bin/pstree ${MPSARGS} ${myPPID}
else
	#exec /usr/bin/pstree ${MPSARGS}|col -b
	/usr/bin/pstree ${MPSARGS}
fi
