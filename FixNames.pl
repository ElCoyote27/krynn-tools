#!/usr/bin/env perl
# Version: $Id: FixNames.pl,v 1.10 2008/01/11 13:57:09 root Exp $
#
# rename script examples from lwall:
#       rename 's/\.orig$//' *.orig
#       rename 'y/A-Z/a-z/ unless /^Make/' *
#       rename '$_ .= ".bad"' *.f
#       rename 'print "$_: "; s/foo/bar/ if <stdin> =~ /^y/i' *

use File::Find;

sub new_name {
	$transform_op =
		's/ /_/g;
		s/é/e/g;
		s/\303\253/e/g;
		s/\303\251/e/g;
		s/\303\250/e/g;
		s/\303\240/a/g;
		s/è/e/g;
		s/à/a/g;
		s/á/a/g;
		s/û/u/g;
		s/ê/e/g;
		s/â/a/g;
		s/\376//g;
		s/&/_/g;
		s/\\\/_/g;
		s/!/_/g;
		s/#/_/g;
		s/\*/_/g;
		s/`/_/g;
		s/\"/_/g;
		s/\(/_/g;
		s/\)/_/g;
		s/,/_/g;
		s/\[/-_/g;
		s/\]/_/g;
		s/__/_/g;
		s/_\.([A-z]*$)/\.\1/g;
		s/\'/_/g';

	$old_file_name = $_;
	eval $transform_op;
	$new_file_name = $_;
	die $@ if $@;
	if ( $old_file_name eq $new_file_name ) {
		# print("Files \"$old_file_name\" and \"$new_file_name\" are identical!\n");
	} else {
		print("\"$old_file_name\" -> \"$new_file_name\"\n");
		rename($old_file_name,$new_file_name) unless $old_file_name eq $new_file_name;
	}

}



sub Main {
	@ARGV = qw(.) unless @ARGV;

	for (@ARGV) {
		new_name($_);
	}
}

&Main();
