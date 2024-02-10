import os
import re

from conda_forge_tick.migrators.core import MiniMigrator
from conda_forge_tick.migrators.libboost import _replacer, _slice_into_output_sections

pat_stub = re.compile(r"(c|cxx|fortran)_compiler_stub")
rgx_idt = r"(?P<indent>\s*)-\s*"
rgx_pre = r"(?P<compiler>\{\{\s*compiler\([\"\']"
rgx_post = r"[\"\']\)\s*\}\})"
rgx_sel = r"(?P<selector>\s*\#\s+\[[\w\s()<>!=.,\-\'\"]+\])?"

pat_compiler_c = re.compile("".join([rgx_idt, rgx_pre, "c", rgx_post, rgx_sel]))
pat_compiler_m2c = re.compile("".join([rgx_idt, rgx_pre, "m2w64_c", rgx_post, rgx_sel]))
pat_compiler_other = re.compile(
    "".join([rgx_idt, rgx_pre, "(m2w64_)?(cxx|fortran)", rgx_post, rgx_sel])
)
pat_compiler = re.compile(
    "".join([rgx_idt, rgx_pre, "(m2w64_)?(c|cxx|fortran)", rgx_post, rgx_sel])
)
pat_stdlib = re.compile(r".*\{\{\s*stdlib\([\"\']c[\"\']\)\s*\}\}.*")
# no version other than 2.17 currently available (except 2.12 as default on linux-64)
pat_sysroot_217 = re.compile(r"- sysroot_linux-64\s*=?=?2\.17")


def _process_section(name, attrs, lines):
    """
    Migrate requirements per section.

    We want to migrate as follows:
    - if there's _any_ `{{ stdlib("c") }}` in the recipe, abort (consider it migrated)
    - if there's `{{ compiler("c") }}` in build, add `{{ stdlib("c") }}` in host
    - where there's no host-section, add it

    If we find `sysroot_linux-64 2.17`, remove those lines and write the spec to CBC.
    """
    write_stdlib_to_cbc = False
    outputs = attrs["meta_yaml"].get("outputs", [])
    global_reqs = attrs["meta_yaml"].get("requirements", {})
    if name == "global":
        reqs = global_reqs
    else:
        filtered = [o for o in outputs if o["name"] == name]
        if len(filtered) == 0:
            raise RuntimeError(f"Could not find output {name}!")
        reqs = filtered[0].get("requirements", {})

    build_reqs = reqs.get("build", set()) or set()
    global_build_reqs = global_reqs.get("build", set()) or set()

    # either there's a compiler in the output we're processing, or the
    # current output has no build-section but relies on the global one
    needs_stdlib = any(pat_stub.search(x or "") for x in build_reqs)
    needs_stdlib |= not bool(build_reqs) and any(
        pat_stub.search(x or "") for x in global_build_reqs
    )
    # see more computation further down depending on dependencies
    # ignored due to selectors, where we need the line numbers below.

    line_build = line_compiler_c = line_compiler_m2c = line_compiler_other = 0
    line_host = line_run = line_constrain = line_test = 0
    indent_c = indent_m2c = indent_other = ""
    selector_c = selector_m2c = selector_other = ""
    last_line_was_build = False
    for i, line in enumerate(lines):
        if re.match(r"^\s*build:.*", line):
            # we need to avoid build.{number,...}, but cannot use multiline
            # regexes here. So leave a marker that we can skip on
            last_line_was_build = True
            line_build = i
        elif pat_compiler_c.search(line):
            line_compiler_c = i
            indent_c = pat_compiler_c.match(line).group("indent")
            selector_c = pat_compiler_c.match(line).group("selector") or ""
        elif pat_compiler_m2c.search(line):
            line_compiler_m2c = i
            indent_m2c = pat_compiler_m2c.match(line).group("indent")
            selector_m2c = pat_compiler_m2c.match(line).group("selector") or ""
        elif pat_compiler_other.search(line):
            line_compiler_other = i
            indent_other = pat_compiler_other.match(line).group("indent")
            selector_other = pat_compiler_other.match(line).group("selector") or ""
        elif re.match(r"^\s*host:.*", line):
            line_host = i
        elif re.match(r"^\s*run:.*", line):
            line_run = i
        elif re.match(r"^\s*run_constrained:.*", line):
            line_constrain = i
        elif re.match(r"^\s*test:.*", line):
            line_test = i
            # ensure we don't read past test section (may contain unrelated deps)
            break
        elif last_line_was_build:
            keys_after_nonreq_build = [
                "binary_relocation",
                "force_ignore_keys",
                "ignore_run_exports(_from)?",
                "missing_dso_whitelist",
                "noarch",
                "number",
                "run_exports",
                "script",
                "skip",
            ]
            if re.match(rf"^\s*({'|'.join(keys_after_nonreq_build)}):.*", line):
                # last match was spurious, reset line_build
                line_build = 0
            last_line_was_build = False

    if line_build:
        # double-check whether there are compilers in the build section
        # that may have gotten ignored by selectors; we explicitly only
        # want to match with compilers in build, not host or run
        build_reqs = lines[
            line_build : (line_host or line_run or line_constrain or line_test or -1)
        ]
        needs_stdlib |= any(pat_compiler.search(line) for line in build_reqs)

    if not needs_stdlib:
        if any(pat_sysroot_217.search(line) for line in lines):
            # if there are no compilers, but we still find sysroot_linux-64,
            # replace it; remove potential selectors, as only that package is
            # linux-only, not the requirement for a c-stdlib
            from_this, to_that = "sysroot_linux-64.*", '{{ stdlib("c") }}'
            lines = _replacer(lines, from_this, to_that, max_times=1)
            lines = _replacer(lines, "sysroot_linux-64.*", "")
            write_stdlib_to_cbc = True
        # otherwise, no change
        return lines, write_stdlib_to_cbc

    # in case of several compilers, prefer line, indent & selector of c compiler
    line_compiler = line_compiler_c or line_compiler_m2c or line_compiler_other
    indent = indent_c or indent_m2c or indent_other
    selector = selector_c or selector_m2c or selector_other
    if indent == "":
        # no compiler in current output; take first line of section as reference (without last \n);
        # ensure it works for both global build section as well as for `- name: <output>`.
        indent = (
            re.sub(r"^([\s\-]*).*", r"\1", lines[0][:-1]).replace("-", " ") + " " * 4
        )

    # align selectors between {{ compiler(...) }} with {{ stdlib(...) }}
    selector = "  " + selector if selector else ""
    to_insert = indent + '- {{ stdlib("c") }}' + selector + "\n"
    if line_build == 0:
        # no build section, need to add it
        to_insert = indent[:-2] + "build:\n" + to_insert

    # if there's no build section, try to insert (in order of preference)
    # before the sections for host, run, run_constrained, test
    line_insert = line_host or line_run or line_constrain or line_test
    if not line_insert:
        raise RuntimeError("Don't know where to insert build section!")
    if line_compiler:
        # by default, we insert directly after the compiler
        line_insert = line_compiler + 1

    lines = lines[:line_insert] + [to_insert] + lines[line_insert:]
    if line_compiler_c and line_compiler_m2c:
        # we have both compiler("c") and compiler("m2w64_c"), likely with complementary
        # selectors; add a second stdlib line after m2w64_c with respective selector
        selector_m2c = " " * 8 + selector_m2c if selector_m2c else ""
        to_insert = indent + '- {{ stdlib("c") }}' + selector_m2c + "\n"
        line_insert = line_compiler_m2c + 1 + (line_compiler_c < line_compiler_m2c)
        lines = lines[:line_insert] + [to_insert] + lines[line_insert:]

    # check if someone specified a newer sysroot in recipe already,
    # leave indicator to migrate() function that we need to write spec to CBC
    if any(pat_sysroot_217.search(line) for line in lines):
        write_stdlib_to_cbc = True
    # as we've already inserted a stdlib-jinja next to the compiler,
    # simply remove any remaining occurrences of sysroot_linux-64
    lines = _replacer(lines, "sysroot_linux-64.*", "")
    return lines, write_stdlib_to_cbc


class StdlibMigrator(MiniMigrator):
    def filter(self, attrs, not_bad_str_start=""):
        lines = attrs["raw_meta_yaml"].splitlines()
        already_migrated = any(pat_stdlib.search(line) for line in lines)
        has_compiler = any(pat_compiler.search(line) for line in lines)
        has_sysroot = any(pat_sysroot_217.search(line) for line in lines)
        # filter() returns True if we _don't_ want to migrate
        return already_migrated or not (has_compiler or has_sysroot)

    def migrate(self, recipe_dir, attrs, **kwargs):
        outputs = attrs["meta_yaml"].get("outputs", [])

        new_lines = []
        write_stdlib_to_cbc = False
        fname = os.path.join(recipe_dir, "meta.yaml")
        if os.path.exists(fname):
            with open(fname) as fp:
                lines = fp.readlines()

            sections = _slice_into_output_sections(lines, attrs)
            for name, section in sections.items():
                # _process_section returns list of lines already
                chunk, cbc = _process_section(name, attrs, section)
                new_lines += chunk
                write_stdlib_to_cbc |= cbc

            with open(fname, "w") as fp:
                fp.write("".join(new_lines))

        fname = os.path.join(recipe_dir, "conda_build_config.yaml")
        if write_stdlib_to_cbc:
            with open(fname, "a") as fp:
                # append ("a") to existing CBC (or create it if it exista already),
                # no need to differentiate as no-one is using c_stdlib_version yet;
                # selector can just be linux as that matches default on aarch/ppc
                fp.write(
                    '\nc_stdlib_version:   # [linux]\n  - "2.17"          # [linux]\n'
                )
