#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1+

import argparse
import mailbox
import os
import os.path
import re
import sys
import tempfile

from .helpers import run_wrapper

try:
    import argcomplete
except ImportError:
    pass

args = None
subject_regex_str = r"\[PATCH *(?P<project>[\w-]*)? *(?P<version>v[0-9]*)? *(?P<number>[0-9]+/[0-9]*)? *\] (?P<title>.*)$"
subject_regex = re.compile(subject_regex_str, re.MULTILINE)
editor = run_wrapper("EDITOR", "vim")


class Patch:
    def __init__(self, msg, match):
        self.msg = msg

        number = match.group("number")
        if number:
            self.number, self.total = (int(x) for x in number.strip().split('/'))
        else:
            self.number = 1
            self.total = 1

        self.project = match.group("project")
        self.version = match.group("version")
        self.title = match.group("title").strip()

        # transliterate
        self.filename = self.title.translate({
            ord(" "): "-", ord(":"): "-", ord("/"): "-", ord("*"): "-",
            ord("("): "-", ord(")"): "-", ord("+"): "-", ord("["): "-",
            ord("]"): "-"
        })
        # remove duplicates and dash in the end
        self.filename = re.sub(r"--+", r"-", self.filename)
        self.filename = self.filename.strip('-')

        self.filename = self.filename + '.patch'

    def __str__(self):
        return self.title

    def parse(msg):
        match = subject_regex.search(msg["subject"])
        if match:
            return Patch(msg, match)
        for alt in args.allow_prefixes:
            alt_subject_str = subject_regex_str.replace("PATCH", alt)
            match = re.search(alt_subject_str, msg["subject"], re.MULTILINE)
            if match:
                return Patch(msg, match)

        return None


class PatchSeries:
    def __init__(self, patches):
        self.patches = patches
        self.total = None
        self.coverletter = None

    def sanitize(self):
        checks = [
            self._sanity_check_same_total,
            self._sanity_check_one_coverletter,
            self._sanity_check_len_ok
        ]

        for c in checks:
            ok, should_retry = c()
            if not ok:
                return (False, should_retry)

        return (True, True)

    # Total, if exists, is the same on all patches
    # Return a tuple (pass, should_retry)
    def _sanity_check_same_total(self):
        total = self.patches[0].total
        for p in self.patches[1:]:
            if p.total != total:
                return (False, self._fixup_all_subjects("Patch '%s' has a different total %d" % (p.title, p.total)))

        self.total = total

        return (True, True)

    # There's only one coverletter
    # Return a tuple (pass, should_retry)
    def _sanity_check_one_coverletter(self):
        for p in self.patches:
            if p.number == 0:
                if self.coverletter:
                    return (False, self._fixup_all_subjects("Patch '%s' and '%s' are coverletters" % (p.title, self.coverletter.title)))

                self.coverletter = p

        return (True, True)

    # total == len(mbox) or total == len(mbox) - 1 when we have a coverletter
    # Return a tuple (pass, should_retry)
    def _sanity_check_len_ok(self):
        if self.total is not None:
            x = self.total
            if self.coverletter:
                x = x + 1
            if len(self.patches) != x:
                print("Number of patches don't match total: %d vs %d" % (len(self.patches), x), file=sys.stderr)
                return (False, False)

        return (True, True)

    def _fixup_all_subjects(self, errmsg):
        print(errmsg, file=sys.stderr)
        if not args.interactive:
            return False

        try:
            subjects = []
            f = tempfile.NamedTemporaryFile("w+")
            f.write("""#
# Error:
#
# %s
#
# These are the subjects from the patches in %s in the order they were received

""" % (errmsg, args.mbox))
            for p in self.patches:
                # we want the real subject from the email to fix it up
                s = p.msg["subject"].strip().translate({ord("\n"): None})
                f.write("\n# (%d/%d) %s\n" % (p.number, p.total, p.title))
                f.write(s)
                subjects.append(s)
            f.flush()
            editor(f.name)

            # now read it back and compare subjects
            f.seek(0)
            idx = 0
            changed = False
            for l in f:
                s = l.strip("\n").strip()
                if not s or s.startswith("#"):
                    continue

                if idx >= len(subjects):
                    # we would add a new patch... force aborting below
                    idx += 1
                    break

                if subjects[idx] != s:
                    changed = True
                    subjects[idx] = s
                idx += 1

            if idx != len(subjects):
                print("\nAborting: new number of patches doesn't match previous",
                      file=sys.stderr)
                return False
            if not changed:
                print("\nAborting: subjects kept the same",
                      file=sys.stderr)
                return False

            idx = 0
            for p in self.patches:
                del p.msg["subject"]
                p.msg["subject"] = subjects[idx]
                idx += 1

            patches = [Patch.parse(p.msg) for p in self.patches]
            self.patches = patches

            return True
        finally:
            f.close()

        return False

    def sort(self):
        if (len(self.patches) != 1):
            self.patches = sorted(self.patches, key=lambda p: p.number)


def parse_args(cmd_args):
    global args

    parser = argparse.ArgumentParser(
        description="Prepare a mbox for use by git - improved version over GIT-MAILSPLIT(1)")

    parser.add_argument(
        "-o", "--output", help="Directory in which to place final patches",
        metavar="DIR",
        default=".")
    parser.add_argument(
        "-p", "--allow-prefixes", help="Besides \"PATCH\" as prefix, allow any of the PREFIX to appear in the subject",
        nargs='+',
        metavar="PREFIX",
        default=[])
    parser.add_argument(
        "-i", "--interactive", help="Allow to interactively fixup patch subjects if mbox-prepare is not able to parse",
        action="store_true")

    group = parser.add_argument_group("Required arguments")
    group.add_argument("mbox", help="mbox file to process", metavar="MBOX_FILE")

    try:
        argcomplete.autocomplete(parser)
    except NameError:
        pass
    args = parser.parse_args(cmd_args)


def main(*cmd_args):
    parse_args(cmd_args)

    box = mailbox.mbox(args.mbox)
    if box is None or len(box) == 0:
        print("No emails in mailbox '%s'?" % args.mbox)
        return 1

    patches = []
    for msg in box:
        p = Patch.parse(msg)
        if not p:
            print("Could not parse subject '%s'" % msg["subject"], file=sys.stderr)
            return 1
        patches.append(p)

    retry = True
    series = PatchSeries(patches)

    while retry:
        ok, retry = series.sanitize()
        if ok:
            retry = False
        elif not retry:
            return 1

    series.sort()

    os.makedirs(args.output, exist_ok=True)

    idx = 1
    for p in series.patches:
        if p == series.coverletter:
            continue
        fn = "%04d-%s" % (idx, p.filename)
        fn = os.path.join(args.output, fn)
        with open(fn, "w") as f:
            f.write("""From: %s
Date: %s
Subject: [PATCH] %s

""" % (p.msg["from"], p.msg["date"], p.title))
            f.write(p.msg.get_payload())
        print(fn)
        idx += 1

    return 0
