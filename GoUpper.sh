#!/bin/bash
#
# $Id: GoUpper.sh,v 1.6 2012/01/04 14:44:59 root Exp $
#
TR_CMD=/usr/bin/tr

${TR_CMD} '[:upper:]' '[:lower:]' | \
awk '# caps - capitalize 1st letter of 1st word

# initialize strings
BEGIN {
	upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        lower = "abcdefghijklmnopqrstuvwxyz" 
	FS = "_|-| " ; OFS = "_"
}

# for each input line
{
	# Print record
	ORIG = $0

for ( i = 1 ; i <= NF; i++)
{
	FIRSTCHAR = substr($i, 1, 1)
	if (CHAR = index(lower, FIRSTCHAR)) 
		$i = substr(upper, CHAR, 1) substr($i, 2)
}

# print record
	print $0
# print record with original
	# print ORIG " " $0
}'
