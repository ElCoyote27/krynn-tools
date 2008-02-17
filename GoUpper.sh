#!/bin/bash
#
# $Id: GoUpper.sh,v 1.2 2008/02/17 19:51:25 root Exp $
#

awk '# caps - capitalize 1st letter of 1st word

# initialize strings
BEGIN {
	upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        lower = "abcdefghijklmnopqrstuvwxyz" 
	FS = "_" ; OFS = "_"
}

# for each input line
{
	# Print record
	ORIG = $0

for ( i = 1 ; i <= NF; i++)
{
# get first character of first word
	FIRSTCHAR = substr($i, 1, 1)
# get position of FIRSTCHAR in lowercase array; if 0, ignore
	if (CHAR = index(lower, FIRSTCHAR)) 
		$i = substr(upper, CHAR, 1) substr($i, 2)
}
# print record
	print ORIG " " $0
}'
