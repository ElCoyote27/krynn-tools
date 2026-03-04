#!/usr/bin/env perl
# Version: $Id: FixNames.pl,v 2.0 2026/03/04 12:00:00 root Exp $
#
# Rename files: strip accents, replace special chars with underscores.
# Uses proper Unicode decomposition instead of raw byte matching.
#
# Usage: FixNames.pl file1 file2 ...
#        FixNames.pl *

use strict;
use warnings;
use Encode qw(decode encode FB_DEFAULT);
use Unicode::Normalize qw(NFKD);

sub new_name {
	my $old = $_;

	my $name = eval { decode('UTF-8', $old, FB_DEFAULT) } // $old;

	# NFKD: decomposes accented chars (e -> e + combining accent),
	# and normalizes fullwidth chars to ASCII equivalents
	$name = NFKD($name);

	# Strip all combining marks (accents, diacritics, etc.)
	$name =~ s/\p{Mark}//g;

	# Smart quotes and apostrophes -> underscore
	$name =~ s/[\x{2018}\x{2019}\x{02BC}]/_/g;
	$name =~ s/[\x{201C}\x{201D}]/_/g;

	# Dashes: en-dash / em-dash -> hyphen
	$name =~ s/[\x{2013}\x{2014}]/-/g;

	# Non-breaking and exotic spaces -> underscore
	$name =~ s/[\x{00A0}\x{2002}-\x{200B}]/_/g;

	# Ellipsis character -> three dots
	$name =~ s/\x{2026}/.../g;

	# Standard replacements (same as original script)
	$name =~ s/ /_/g;
	$name =~ s/&/_/g;
	$name =~ s/\\/_/g;
	$name =~ s/!/_/g;
	$name =~ s/#/_/g;
	$name =~ s/\*/_/g;
	$name =~ s/`/_/g;
	$name =~ s/"/_/g;
	$name =~ s/\(/_-_/g;
	$name =~ s/\)/_/g;
	$name =~ s/,/_/g;
	$name =~ s/\[/-_/g;
	$name =~ s/\]/_/g;
	$name =~ s/'/_/g;

	# Catch-all: replace any remaining non-ASCII with underscore
	$name =~ s/[^\x00-\x7F]/_/g;

	# Clean up multiple underscores, leading/trailing junk
	$name =~ s/__+/_/g;
	$name =~ s/^_//;
	$name =~ s/^-//;
	$name =~ s/_$//;
	$name =~ s/_\.([A-Za-z0-9]*$)/.$1/;

	$name = encode('UTF-8', $name);

	if ($old ne $name) {
		print("\"$old\" -> \"$name\"\n");
		rename($old, $name);
	}
}

sub Main {
	@ARGV = qw(.) unless @ARGV;
	for (@ARGV) {
		new_name($_);
	}
}

Main();
