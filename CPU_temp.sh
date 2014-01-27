#!/bin/bash
# $Id: CPU_temp.sh,v 1.7 2014/01/27 20:14:49 root Exp $
CPUTMP_FILE=`/bin/mktemp -p /tmp --suffix=CPU_temp`

if [ ! -f ${CPUTMP_FILE} ]; then
	echo "Ooops!"; exit 127
fi

if [ -x /usr/bin/sensors ]; then
	/usr/bin/sensors > ${CPUTMP_FILE}
	TEMPS=`grep Core ${CPUTMP_FILE}|(while read a core temp scale max; do echo $temp; done)|sort -ur`
else
	echo "/usr/bin/sensors not found!"
fi

# IPMITOOL
if [ -x /usr/bin/ipmitool ] ; then
#	AMB_TEMP=`/usr/bin/ipmitool sdr list|awk -F '|' '{ if (( $1 ~ /Ambient/ ) && ( $3 ~ /ok/ )) print $2}'`
	AMB_TEMP=`/usr/sbin/ipmi-sensors -t Temperature --ignore-not-available-sensors|awk -F '|' '{ if ( $2 ~ /Ambient/ ) print $4,$5}'|xargs`
	if [ "x${AMB_TEMP}" != "x" ]; then
		echo "Ambient Temp: ${AMB_TEMP}"
	fi
fi

# Iterate
for mytemp in $TEMPS
do
	CPU_CORES=`grep "Core.*${mytemp} C" ${CPUTMP_FILE}|awk '{ print $2}'|sed -e 's/://'|xargs|sed -e 's/ /,/g'`
	MAX_TEMP=`grep "Core.*${mytemp} C" ${CPUTMP_FILE}|awk '{ print $5,$6,$7,$8,$9,$10,$11,$12}'|sort -u`
	echo "Temp: $mytemp C $MAX_TEMP, CPU Cores: ${CPU_CORES}"
done
rm -f ${CPUTMP_FILE}
