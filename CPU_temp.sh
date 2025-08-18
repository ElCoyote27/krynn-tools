#!/bin/bash
# $Id: CPU_temp.sh,v 1.13 2019/01/16 18:13:22 root Exp $
#
export LC_ALL=C

#
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
	/usr/bin/ipmitool sdr info > /dev/null 2>&1
	if [ $? -eq 0 ]; then
		#AMB_TEMP=`/usr/bin/ipmitool sdr list|awk -F '|' '{ if (( $1 ~ /Ambient/ ) && ( $3 ~ /ok/ )) print $2}'`
		#AMB_TEMP=`/usr/sbin/ipmi-sensors -t Temperature --ignore-not-available-sensors|awk -F '|' '{ if ( $2 ~ /Ambient/ ) print $4,$5}'|xargs`
		#AMB_TEMP=`/usr/sbin/ipmi-sensors -s 10 --ignore-not-available-sensors|awk -F '|' '{ if ( $2 ~ /Ambient/ ) print $4,$5}'|xargs`
		AMB_TEMP=$(/usr/sbin/ipmi-sensors -t Temperature|awk '{ if ( $3 ~ /Inlet/ ) print $8}')
		FAN_NAME=$(ipmitool sdr type Fan|head -1|awk '{ print $1}')
		CUR_RPM=$(/usr/bin/ipmitool sensor get ${FAN_NAME}|awk 'BEGIN { FS=":" } {  if ( $2 ~ /RPM/ ) { print $2 } }'|head -1)
	        GOOD_MSG="(II) MAX T: ${MAX_TEMP}C, Current T: ${CUR_TEMP}C, Fan: ${CUR_RPM}"
		if [ "x${AMB_TEMP}" != "x" ]; then
			echo "(II) Ambient Temp: ${AMB_TEMP}"
		fi
		if [[ ! -z ${CUR_RPM} ]]; then
			echo "(II) Fan: ${CUR_RPM}"
		else
			echo "(II) Fan: N/A"
		fi
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
