"""
Implementation of the conda-diff-tar command, a tools which allows creating
differential tarballs of a (usually mirrored) conda repository.  The resulting
tarball can be used to update a copy of the mirror on a remote (air-gapped)
system, without having to copy the entire conda repository.
"""
import os
import sys
import json
import hashlib
import tarfile
from os.path import abspath, isdir, join, relpath


DEFAULT_REFERENCE_PATH = "./reference.json"
DEFAULT_UPDATE_PATH = "./update.tar"


class NoReferenceError(FileNotFoundError):
    pass


def md5_file(path):
    """
    Return the MD5 hashsum of the file given by `path` in hexadecimal
    representation.
    """
    h = hashlib.new("md5")
    with open(path, "rb") as fi:
        while 1:
            chunk = fi.read(262144)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_repos(mirror_dir):
    """
    Given the path to a directory, iterate all sub-directories
    which contain a repodata.json and repodata.json.bz2 file.
    """
    for root, unused_dirs, files in os.walk(mirror_dir):
        if "repodata.json" in files and "repodata.json.bz2" in files:
            yield root


def all_repodata(mirror_dir):
    """
    Given the path to a directory, return a dictionary mapping all repository
    sub-directories to the conda package list as respresented by
    the 'packages' field in repodata.json.
    """
    d = {}
    for repo_path in find_repos(mirror_dir):
        with open(join(repo_path, "repodata.json")) as fi:
            index = json.load(fi)["packages"]
        d[repo_path] = index
    return d


def verify_all_repos(mirror_dir):
    """
    Verify all the MD5 sum of all conda packages listed in all repodata.json
    files in the repository.
    """
    d = all_repodata(mirror_dir)
    for repo_path, index in d.items():
        for fn, info in index.items():
            path = join(repo_path, fn)
            if info["md5"] == md5_file(path):
                continue
            print("MD5 mismatch: %s" % path)


def write_reference(mirror_dir, outfile=None):
    """
    Write the "reference file", which is a collection of the content of all
    repodata.json files.
    """
    if not outfile:
        outfile = DEFAULT_REFERENCE_PATH
    data = json.dumps(all_repodata(mirror_dir), indent=2, sort_keys=True)
    # make sure we have newline at the end
    if not data.endswith("\n"):
        data += "\n"
    with open(outfile, "w") as fo:
        fo.write(data)


def read_reference(infile=None):
    """
    Read the "reference file" from disk and return its content as a dictionary.
    """
    if not infile:
        infile = DEFAULT_REFERENCE_PATH
    try:
        with open(infile) as fi:
            return json.load(fi)
    except FileNotFoundError as e:
        raise NoReferenceError(e)


def get_updates(mirror_dir, infile=None):
    """
    Compare the "reference file" to the actual the repository (all the
    repodata.json files) and iterate the new and updates files in the
    repository.  That is, the files which need to go into the differential
    tarball.
    """
    if not infile:
        infile = DEFAULT_REFERENCE_PATH
    d1 = read_reference(infile)
    d2 = all_repodata(mirror_dir)
    for repo_path, index2 in d2.items():
        index1 = d1.get(repo_path, {})
        if index1 != index2:
            for fn in "repodata.json", "repodata.json.bz2":
                yield relpath(join(repo_path, fn), mirror_dir)
        for fn, info2 in index2.items():
            info1 = index1.get(fn, {})
            if info1.get("md5") != info2["md5"]:
                yield relpath(join(repo_path, fn), mirror_dir)


def tar_repo(mirror_dir, infile=None, outfile=None, verbose=False):
    """
    Write the so-called differential tarball, see get_updates().
    """
    if not infile:
        infile = DEFAULT_REFERENCE_PATH
    if not outfile:
        outfile = DEFAULT_UPDATE_PATH
    t = tarfile.open(outfile, "w")
    for f in get_updates(mirror_dir, infile):
        if verbose:
            print("adding: %s" % f)
        t.add(join(mirror_dir, f), f)
    t.close()
    if verbose:
        print("Wrote: %s" % outfile)


def main():
    import argparse

    p = argparse.ArgumentParser(
        description='create "differential" tarballs of a conda repository'
    )

    p.add_argument(
        "repo_dir",
        nargs="?",
        action="store",
        metavar="REPOSITORY",
        help="path to repository directory",
    )

    p.add_argument("--create", action="store_true", help="create differential tarball")

    p.add_argument(
        "--reference", action="store_true", help="create a reference point file"
    )

    p.add_argument(
        "-o",
        "--outfile",
        action="store",
        help="Path to references json file when using --reference, "
        "or update tarfile when using --create",
    )

    p.add_argument(
        "-i",
        "--infile",
        action="store",
        help="Path to specify references json file when using --create or --show",
    )

    p.add_argument(
        "--show",
        action="store_true",
        help="show the files in respect to the latest reference "
        "point file (which would be included in the "
        "differential tarball)",
    )

    p.add_argument(
        "--verify", action="store_true", help="verify the mirror repository and exit"
    )

    p.add_argument("-v", "--verbose", action="store_true")

    p.add_argument("--version", action="store_true", help="print version and exit")

    args = p.parse_args()

    if args.version:
        from conda_mirror import __version__

        print("conda-mirror: %s" % __version__)
        return

    if not args.repo_dir:
        p.error("exactly one REPOSITORY is required, try -h")

    mirror_dir = abspath(args.repo_dir)
    if not isdir(mirror_dir):
        sys.exit("No such directory: %r" % mirror_dir)

    try:
        if args.create:
            if args.outfile:
                outfile = args.outfile
            else:
                outfile = DEFAULT_UPDATE_PATH

            if args.infile:
                infile = args.infile
            else:
                infile = DEFAULT_REFERENCE_PATH

            tar_repo(mirror_dir, infile, outfile, verbose=args.verbose)

        elif args.verify:
            verify_all_repos(mirror_dir)

        elif args.show:
            if args.infile:
                infile = args.infile
            else:
                infile = DEFAULT_REFERENCE_PATH

            if args.outfile:
                p.error("--outfile not allowed with --show")

            for path in get_updates(mirror_dir, infile):
                print(path)

        elif args.reference:
            if args.infile:
                p.error("--infile not allowed with --reference")
            if args.outfile:
                outfile = args.outfile
            else:
                outfile = DEFAULT_REFERENCE_PATH

            write_reference(mirror_dir, outfile)

        else:
            print("Nothing done.")

    except NoReferenceError:
        sys.exit(
            """\
Error: no such file: %s
Please use the --reference option before creating a differential tarball.\
"""
            % DEFAULT_REFERENCE_PATH
        )


if __name__ == "__main__":
    main()
