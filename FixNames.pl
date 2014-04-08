#!/usr/bin/env perl
# Version: $Id: FixNames.pl,v 1.19 2014/04/08 22:35:07 root Exp $
#
# WARNING: Edit with 'vi', not 'pico'!!
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
		s/\303\200/A/g; #A
		s/\303\201/A/g;
		s/\303\202/A/g;
		s/\303\203/A/g;
		s/\303\204/A/g;
		s/\303\205/A/g;
		s/\303\207/C/g; #C
		s/\303\213/E/g; #E
		s/\303\212/E/g;
		s/\303\211/E/g;
		s/\303\210/E/g;
		s/\303\217/I/g; #I
		s/\303\216/I/g;
		s/\303\215/I/g;
		s/\303\214/I/g;
		s/\303\227/A/g; #A
		s/\303\226/O/g; #O
		s/\303\225/O/g;
		s/\303\224/O/g;
		s/\303\223/O/g;
		s/\303\222/O/g;
		s/\303\253/e/g; #e
		s/\303\252/e/g;
		s/\303\251/e/g;
		s/\303\250/e/g;
		s/\303\257/i/g; #i
		s/\303\256/i/g;
		s/\303\255/i/g;
		s/\303\254/i/g;
		s/\303\243/a/g; #a
		s/\303\242/a/g;
		s/\303\241/a/g;
		s/\303\240/a/g;
		s/\303\266/o/g; #o
		s/\303\265/o/g;
		s/\303\264/o/g;
		s/\303\263/o/g;
		s/\303\262/o/g;
		s/\303\261/o/g;
		s/\303\260/o/g;
		s/\303\261/n/g; #n
		s/\303\274/u/g; #u
		s/\303\273/u/g;
		s/\303\272/u/g;
		s/\303\271/u/g;
		s/\302\257/o/g; #o
		s/\302\260/o/g;
		s/\200/e/g;
		s/\201/e/g;
		s/\202/e/g;
		s/\203/e/g;
		s/\204/i/g;
		s/\205/i/g;
		s/\206/i/g;
		s/\207/i/g;
		s/\210/e/g;
		s/\211/o/g;
		s/\212/o/g;
		s/\213/o/g;
		s/\214/o/g;
		s/\216/o/g;
		s/\217/u/g;
		s/\304\207/c/g;
		s/u\314/u/g;
		s/\340/a/g;
		s/\341/a/g;
		s/\342/a/g;
		s/\343/a/g;
		s/\344/a/g;
		s/\345/a/g;
		s/\346/a/g;
		s/\350/e/g;
		s/\351/e/g;
		s/\352/e/g;
		s/\353/e/g;
		s/\354/i/g;
		s/\355/i/g;
		s/\356/i/g;
		s/\357/i/g;
		s/\362/o/g;
		s/\363/o/g;
		s/\364/o/g;
		s/\365/o/g;
		s/\366/o/g;
		s/\370/o/g;
		s/\371/u/g;
		s/\372/u/g;
		s/\373/u/g;
		s/\374/u/g;
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
		s/^_//g;
		s/^-//g;
		s/_$//g;
		s/_\.([A-z]*$)/\.\1/g;
		s/\'/_/g';

	$old_file_name = $_;
	eval $transform_op; # Do it several times, just to make really sure...
	eval $transform_op;
	eval $transform_op;
	eval $transform_op;
	eval $transform_op;
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
