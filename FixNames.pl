#!/usr/bin/env perl
# Version: $Id: FixNames.pl,v 1.11 2008/02/28 15:58:52 root Exp $
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
		s/\302\257/o/g;
		s/\303\207/e/g;
		s/\303\253/e/g;
		s/\303\251/e/g;
		s/\303\250/e/g;
		s/\303\240/e/g;
		s/\200/e/g;
		s/\201/e/g;
		s/\202/e/g;
		s/\203/e/g;
		s/\204/i/g;
		s/\205/i/g;
		s/\206/i/g;
		s/\207/i/g;
		s/\210/o/g;
		s/\211/o/g;
		s/\212/o/g;
		s/\213/o/g;
		s/\214/o/g;
		s/\216/o/g;
		s/\217/u/g;
		s/\370/o/g;
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
