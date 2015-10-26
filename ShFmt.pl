#!/usr/bin/perl
#
# $Id: ShFmt.pl,v 1.2 2010/05/31 13:02:55 root Exp $
#
# fmt.script - format scripts in awk, csh, ksh, perl, sh
#
# we do:
# standardize indentation (an indent is one tab by default)
# strip trailing whitespace
# change ${var} to $var where possible
# change ">x" to "> x" for shell scripts
# change "[ ... ]" to "test ..." for Bourne shell scripts
#
# we may do someday, but these are harder:
# convert $VAR to $var unless a setenv or default environment variable
# possibly prepending stuff from template.sh
# "if ... \nthen", for ... in\ndo", "while/until ... \ndo", "fn()\n{"
#
# to have fmt.script reformat itself (a fair test, yes?) try:
#	fmt.script     fmt.script fmt.script.new	# use tabs for indents
#	fmt.script -s4 fmt.script fmt.script.new	# indent is fou

# variable initialization
$RCSversion = q$Revision: 1.2 $;
($version) = ($RCSversion =~ /Revision:\s+([\d]+\.[\d]+)/);
$pr		= $0;		# name of this program
$pr		=~ s|.*\/||;	# basename of this program
$ilen		= 1;		# characters per indent
$ichar		= "\t";		# indent character (typically <SPACE> or <TAB>)
$ilevel		= 0;		# indent level
$caselevel	= 0;		# case level for sh/ksh scripts
$type		= '';		# unknown type

# usage message
$usage		= "usage:
$pr -help
$pr [-t|-s#] [-ksh|-sh] [infile [outfile]]
-t		indent is one tab character (default)
-s#		indent is <#> space characters
-sh		script is Bourne shell
-ksh		script is Korn shell
infile		input  file name (default STDIN)
outfile		output file name (default STDOUT)
notes:
* you can use '-' for STDIN/STDOUT
* $pr gives outfile execute permissions when possible
example:
* to convert an ugly script into a pretty script, using a 4space indent:
$pr -s4 script.ugly script.pretty
";

# process command options
ARGS: while ($#ARGV >= 0) {
    $_ = $ARGV[0];
    ARG0: {
	/^-h/	    && do { print "$usage"; exit; };		# print usage
	/^-t$/	    && do { $ichar="\t"; $ilen=1 ; last ARG0 }; # one tab
	/^-s(\d+)$/ && do { $ichar=" " ; $ilen=$1; last ARG0 }; # $1 spaces
	/^-sh$/     && do { $type='sh';            last ARG0 };	# Bourne shell
	/^-ksh$/    && do { $type='ksh';           last ARG0 };	# Korn shell
	/./         && last ARGS;				# file to fmt
    }
    shift;
}

# check remaining args
if ($#ARGV > 1 || $ARGV[0] =~ /^-./) {
    die "$usage";
}

#============================================================================
# regular expression stuff
#
# list of regex to increase next line
$flow = '^(if|for|foreach|while|until)\s';	# flow control statements
$caseelem = '^[^\(\)]+\)(\s|$)';	# sh case element (no internal parens!)
$function = '\(\s*\)(\s*\{\s*)?$';	# sh lists, perl functions, code blocks
$list = '[\{\(]\s*$';			# list executed, possibly in subshell
$hereis = '<<\s*(\S+)\s*$';		# hereis documents

# list of regex to decrease this line
$endflow = '^(done|end|endif|fi)\b';	# fi/done/end/endif
$endfn = '^\}';				# end of function declaration or block
$endlist = '^[\}\)]';			# end of list or block execution
$endcaseelem = '^(;;|breaksw)';		# end of sh/csh case elements

# list of regex to decrease this line *and* increase next line
$else = '^(\}\s*)?(else|elif|elsif|else\s+if)\b'; 	# else/elsif variants
@endhereis = ();			# words used to begin/end here-is docs

# list of regex to *postpone* indentation until *next* line
$postponeinc = '^(then|do)\b';		# sh if/for/while/until ... \nthen/do

# list of useful regex - watch out for oneliners like
#	"for ... ; do ... ; done"
#	"case ... in ; ...) ... ;; ...) ... ;; esac"
#	"...) ... ;;		# part of condensed form case statement
# we could even have something like
#	for ... ; do ... ; done | whatever ...
$linecaseelem = "$caseelem.*;;";	# handle condensed form case element
$endinline = ';\s*(done|end|esac|fi)\b'; # inline flow statement end
$falseincnext = "$linecaseelem|$endinline"; # false alarm

# regex arrays @decthis and @incnext notes:
# 1) $caseelem regex is pushed onto @incnext only while inside a sh/ksh case
# 2) $hereis regex is pushed onto @incnext only for sh/ksh/csh scripts
# 3) $else has to go *before* the other regex in @decthis, because of $endfn
@decthis = ($else, $endflow, $endfn, $endlist, $endcaseelem);
@incnext = ($else, $flow, $function, $list);
if ($type =~ /sh$/) {
    push(@incnext, $hereis);
}

#============================================================================


# start our work - open files, etc.
$istr		= $ichar x $ilen; # indent string (typically "    " or "\t")
$input	= shift(@ARGV) || '-';
$output	= shift(@ARGV) || '-';
open( INPUT, "$input")		|| die "$pr: Unable to open $input: $!\n";
open(OUTPUT, ">$output")	|| die "$pr: Unable to create $output: $!\n";

# process input
while ($line = <INPUT>) {

    # initial processing for every line
    $doincnext = 0;		# increase indent on next line?
    $dodecthis = 0;		# decrease indent on this line?
    $dopostponeinc = 0;		# postpone increase indent on this line?
    $badcaseelem = 0;		# do we have a bad case element?
    $comment = '';		# assume this line has no inline comment
    chop($line);		# strip newline
    $line =~ s/\s+$//;		# strip trailing whitespace
    $line =~ s/^\s+//;		# strip indentation

    # blank lines and comment lines can be passed straight through, with
    # no effect on indentation
    if ($line =~ /^$/) {
	print OUTPUT "\n";
	next;
    }
    if ($line =~ /^#/) {
	# figure out what type of script we are processing
	if ($. == 1) {
	    if ($line =~ m,#!\s*/\S+/(\w+)(\s|$), ) {
		$type = $1;
	    }
	}
	print OUTPUT $istr x $ilevel, $line, "\n";
	next;
    }

    # inline comments can be stripped to protect them from other substitutions;
    # the tricky part is deciding what is a comment, since hashmarks can
    # appear inside quotes;
    # we will err on the safe side; meaning we will not strip the comment
    # unless we are pretty sure it is safe
    if ($line =~ s/(\s+#[^'"`]+$)//) {
	$comment = $1;
    }
    # ElCoyote: the other way around... (and skip awk stuff)
    # inline substitutions			# discouraged	preferred
    if ($line =~ /awk/) {
        # $line =~ s/\$\{(\w+)\}(\W|$)/\$$1$2/g;      # ${var}        $var
    } else {
        $line =~ s/\$(\w+)(\W|$)/\$\{$1\}$2/g;      # $var        ${var}
    }
    if ($type =~ /^k?sh$/) {
	$line =~ s,>([^\s\&>]),> $1,;		# x>file	x> file
	$line =~ s,([^\s\d>])>,$1 >,;		# x> file	x > file
	# 					# [ ... ]	test ...
	# $line =~ s/^((if|while)(\s+))?(\[)(.*[^\\"'])(\])/$1test$5/;
    }

    # track case level - use this to see if we should check for case elements
    # there is no point in checking for case elements if we aren't in a
    # ksh/sh script and actually inside a case statement
    if ($type =~ /^k?sh$/) {
	if ($line =~ /^case\b/ && $line !~ /$linecaseelem/) {
	    ($caselevel == 0) && push(@incnext, $caseelem);
	    $caselevel++;
	} elsif ($line =~ /^esac\b/) {
	    $caselevel--;
	    ($caselevel == 0) && pop(@incnext);
	}
    }

    # see if we are going to decrease this line's indentation
    foreach $regex (@decthis) {
	($line =~ /$regex/) && ($dodecthis=1) && last;
    }
    if ($dodecthis == 1) {
	$ilevel--;
    }

    # handle "command ;;" problems, but make sure we don't trip over
    # condensed form case
    if ($type =~ /^k?sh$/ && $line =~ /.;;/ && $line !~ /$linecaseelem/) {
	$badcaseelem = 1;
	$line =~ s/\s*;;//;
    }

    # see if we are going to postpone increasing this line's indentation
    if ($line =~ /^(then|do)\b/) {
	$dopostponeinc = 1;
	$ilevel--;
    }

    # print this line (but don't indent blank lines)
    if (! length($line)) {
	print OUTPUT "\n";
    } else {
	print OUTPUT $istr x $ilevel, $line, $comment, "\n";
    }

    # if we found a bad case element print the closing ";;" which we stripped
    if ($badcaseelem) {
	$ilevel--;
	print OUTPUT $istr x $ilevel, ";;\n";
    }

    # we postponed it above, but make sure we do it now
    if ($dopostponeinc == 1) {
	$ilevel++;
    } else {
	# see if we are going to increase the next line's indentation
	foreach $regex (@incnext) {
	    ($line =~ /$regex/) && ($doincnext=1) && last;
	}
	if ($doincnext == 1) {
	    if ($regex == $flow) {
		# if we are bourne shell, we can do some advanced checking
		if ($type =~ /^k?sh$/) {
		    # for "for" and "case" we can do further checking, but
		    # for "if", "elif", "else", and "while" we usually can't
		    # prove anything; however there is one small exception
		    if ($line =~ /^for/) {
			if ($line !~ /^for\s+\w+($|\s+[;#]|\s+in\s+\S+)/) {
			    $doincnext = 0;
			}
		    } elsif ($line =~ /^case/) {
			if ($line !~ /^case\s+\S+\s+in\b/) {
			    $doincnext = 0;
			}
		    } else {
			# flow keywords can be in quoted multi-line text, like:
			# usage="$pr [opts]\nif you like chocolate, honk!"\n
			# this can sometimes be reliably detected (assuming the
			# code actually runs)
			# if the line has no comments and exactly 1 quote char,
			# it is probably like the usage example above
			if ($line !~ /#/) {
			    @chars = split(//, $line);
			    $nquote = grep(/[\'\"\`]/, @chars);
			    if ($nquote == 1) {
				$doincnext = 0;
			    }
			}
		    }
		}
	    }
	    if ($doincnext == 1 && $line !~ /$falseincnext/) {
		$ilevel++;
	    }
	}
    }
    #print STDERR $pr, ": $doincnext/$dodecthis=$ilevel:", $line, "\n";
}
close(INPUT);
close(STDOUT);

# fix permissions on output file
if ($output ne '-') {
    if ($input ne '-') {
	($dev,$ino,$mode,@junk) = stat($input);
	chmod($mode,$output);
    } else {
	system("chmod +x $output");
    }
}
exit;
