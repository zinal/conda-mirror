import argparse
import bz2
import fnmatch
import hashlib
import json
import logging
import multiprocessing
import os
import pdb
import re
import shutil
import sys
import tarfile
import tempfile
import time
import random
from pprint import pformat
from typing import Any, Callable, Dict, Iterable, Set, Union, List, NamedTuple

import requests
import yaml

from tqdm import tqdm

try:
    from conda.models.version import BuildNumberMatch, VersionSpec, VersionOrder
except ImportError:
    from .versionspec import BuildNumberMatch, VersionSpec, VersionOrder

logger = None

DEFAULT_BAD_LICENSES = ["agpl", ""]

DEFAULT_PLATFORMS = ["linux-64", "linux-32", "osx-64", "win-64", "win-32", "noarch"]

DEFAULT_CHUNK_SIZE = 16 * 1024

# Pattern matching special characters in version/build string matchers.
VERSION_SPEC_CHARS = re.compile(r"[<>=^$!]")


def _maybe_split_channel(channel):
    """Split channel if it is fully qualified.

    Otherwise default to conda.anaconda.org

    Parameters
    ----------
    channel : str
        channel on anaconda, like "conda-forge" or fully qualified channel like
        "https://conda.anacocnda.org/conda-forge"

    Returns
    -------
    download_template : str
        defaults to
        "https://conda.anaconda.org/{channel}/{platform}/{file_name}"
        The base url will be modified if the `channel` input parameter is
        fully qualified
    channel : str
        The name-only channel. If the channel input param is something like
        "conda-forge", then "conda-forge" will be returned. If the channel
        input param is something like "https://repo.continuum.io/pkgs/free/"

    """
    # strip trailing slashes
    channel = channel.strip("/")

    default_url_base = "https://conda.anaconda.org"
    url_suffix = "/{channel}/{platform}/{file_name}"
    if "://" not in channel:
        # assume we are being given a channel for anaconda.org
        logger.debug("Assuming %s is an anaconda.org channel", channel)
        url = default_url_base + url_suffix
        return url, channel
    # looks like we are being given a fully qualified channel
    download_base, channel = channel.rsplit("/", 1)
    download_template = download_base + url_suffix
    logger.debug("download_template=%s. channel=%s", download_template, channel)
    return download_template, channel


def _match(all_packages: Dict[str, Dict[str, Any]], key_pattern_dict: Dict[str, str]):
    """

    Parameters
    ----------
    all_packages : Dictionary mapping package file names to metadata dictionary for that instance.
        Represents package metadata dicts from repodata.json
    key_pattern_dict : Dictionary mapping keys to patterns
        The pattern may either be a glob expression or if the key is 'version' may also
        be a conda version specifier.

    Returns
    -------
    matched : dict
        Iterable of package metadata dicts which match the `target_packages`
        (key, pattern) tuples

    """

    matched = dict()
    matchers: Dict[str, Callable[[Any], bool]] = {}
    for key, pattern in sorted(key_pattern_dict.items()):
        key = key.lower()
        pattern = pattern.lower()
        if key == "version" and VERSION_SPEC_CHARS.search(pattern):
            # If matching the version and the pattern contains one of the characters
            # in '<>=^$!', then use conda's version matcher, otherwise assume a glob.
            if " " in pattern:
                # version spec also contains a build string match
                pattern, build_pattern = pattern.split(" ", maxsplit=1)
                if "build" not in matchers:
                    matchers["build"] = _build_matcher(build_pattern)
            matcher = _version_matcher(pattern)
        elif key == "build" and VERSION_SPEC_CHARS.search(pattern):
            matcher = _build_matcher(pattern)
        else:
            matcher = _glob_matcher(pattern)
        matchers[key] = matcher

    for pkg_name, pkg_info in all_packages.items():
        # normalize the strings so that comparisons are easier
        if all(
            matcher(str(pkg_info.get(key, "")).lower())
            for key, matcher in matchers.items()
        ):
            matched.update({pkg_name: pkg_info})

    return matched


def _glob_matcher(pattern: str) -> Callable[[Any], bool]:
    """Returns a function that will match against given glob expression."""

    def _globmatch(v, p=pattern):
        return fnmatch.fnmatch(v, p)

    return _globmatch


def _version_matcher(pattern: str) -> Callable[[Any], bool]:
    """Returns a function that will match against given conda version specifier."""
    # Throw away build string pattern if present.
    return VersionSpec(pattern.split(" ")[0]).match


def _build_matcher(pattern: str) -> Callable[[Any], bool]:
    """Returns a function that will match against a build string"""
    return BuildNumberMatch(pattern).match


class DependsMatcher:
    """Implements a function that matches against version and build attributes of package info"""

    def __init__(self, pattern: str):
        if not pattern:
            pattern = "*"
        parts = pattern.split(" ", maxsplit=1)
        self._version_matcher = _version_matcher(parts[0])
        if len(parts) > 1:
            self._build_matcher = _build_matcher(parts[1])
        else:
            self._build_matcher = lambda _: True

    def __call__(self, pkg_info: Dict[str, Any]) -> bool:
        return self._version_matcher(
            pkg_info.get("version", "")
        ) and self._build_matcher(pkg_info.get("build", ""))


def _restore_required_dependencies(
    all_packages: Dict[str, Dict[str, Any]],
    excluded: Set[str],
    required: Set[str],
) -> Set[str]:
    """Recursively removes dependencies of required packages from excluded packages.

    This removes from the `excluded` package list any packages that are direct or
    indirect dependencies of the `required` package list. It is assumed that the
    excluded and required sets are disjoint.

    Parameters
    ----------
    all_packages:
        Dictionary mapping package filename to metadata dictionary representing
        contents of repodata.json.
    excluded:
        Initial set of package filenames to be excluded from download.
    required:
        Set of package filenames initially to be included.

    Returns
    -------
    New set of excluded packages with dependencies removed.
    """

    cur_required = set(required)

    # TODO - support platform-specific + noarch

    already_required = set(all_packages.get(r, {}).get("name") for r in required)

    final_excluded: Set[str] = set(excluded)

    while len(final_excluded) > 0 and len(cur_required) > 0:
        required_depend_specs: Dict[str, Set[str]] = {}

        # collate version specs of dependencies package by package name
        for req in cur_required:
            info = all_packages.get(req, {})
            for dep in info.get("depends", ()):
                try:
                    pkg_name, version_spec = dep.split(maxsplit=1)
                except ValueError:
                    pkg_name, version_spec = dep, ""
                if pkg_name not in already_required:
                    required_depend_specs.setdefault(pkg_name, set()).add(version_spec)

        cur_required.clear()

        for k in list(final_excluded):
            info = all_packages.get(k, {})
            pkg_name = info.get("name")
            for version_spec in required_depend_specs.get(pkg_name, ()):
                matcher = DependsMatcher(version_spec)
                if matcher(info):
                    final_excluded.remove(k)
                    cur_required.add(k)
                    break

    return final_excluded


def _str_or_false(x: str) -> Union[str, bool]:
    """
    Returns a boolean False if x is the string "False" or similar.
    Returns the original string otherwise.
    """
    if x.lower() == "false":
        x = False
    return x

def _make_arg_parser():
    """
    Localize the ArgumentParser logic

    Returns
    -------
    argument_parser : argparse.ArgumentParser
        The instantiated argument parser for this CLI
    """
    ap = argparse.ArgumentParser(description="CLI interface for conda-mirror.py")

    ap.add_argument(
        "--upstream-channel",
        help=(
            "The target channel to mirror. Can be a channel on anaconda.org "
            'like "conda-forge" or a full qualified channel like '
            '"https://repo.continuum.io/pkgs/free/"'
        ),
    )
    ap.add_argument(
        "--target-directory",
        help="The place where packages should be mirrored to",
    )
    ap.add_argument(
        "--temp-directory",
        help=(
            "Temporary download location for the packages. Defaults to a "
            "randomly selected temporary directory. Note that you might need "
            "to specify a different location if your default temp directory "
            "has less available space than your mirroring target"
        ),
        default=tempfile.gettempdir(),
    )
    ap.add_argument(
        "--platform",
        action="append",
        default=[],
        help=(
            f"The OS platform(s) to mirror. one of: {', '.join(DEFAULT_PLATFORMS)} "
            "May be specified multiple times."
        ),
    )
    ap.add_argument(
        "-D",
        "--include-depends",
        action="store_true",
        help=("Include packages matching any dependencies of packages in whitelist."),
    )
    ap.add_argument(
        "--latest",
        metavar="<n>",
        type=int,
        nargs="?",
        const=1,
        default=-1,
        help=(
            "Only download most-recent <n> non-dev instance(s) of each package. "
            "If specified then "
        ),
    )
    ap.add_argument(
        "--latest-dev",
        metavar="<n>",
        type=int,
        nargs="?",
        const=1,
        default=-1,
        help="Only download most-recent <n> dev instance(s) of each package.",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        help=(
            "logging defaults to error/exception only. Takes up to three "
            "'-v' flags. '-v': warning. '-vv': info. '-vvv': debug."
        ),
        default=0,
    )
    ap.add_argument(
        "--config",
        action="store",
        help="Path to the yaml config file",
    )
    ap.add_argument(
        "--pdb",
        action="store_true",
        help="Enable PDB debugging on exception",
        default=False,
    )
    ap.add_argument(
        "--num-threads",
        action="store",
        default=1,
        type=int,
        help="Num of threads for validation. 1: Serial mode. 0: All available.",
    )
    ap.add_argument(
        "--version",
        action="store_true",
        help="Print version and quit",
        default=False,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show what will be downloaded and what will be removed. Will not "
            "validate existing packages"
        ),
        default=False,
    )
    ap.add_argument(
        "--no-validate-target",
        action="store_true",
        help="Skip validation of files already present in target-directory",
        default=False,
    )
    ap.add_argument(
        "--minimum-free-space",
        help=("Threshold for free diskspace. Given in megabytes."),
        type=int,
        default=1000,
    )
    ap.add_argument(
        "--proxy",
        help=("Proxy URL to access internet if needed"),
        type=str,
        default=None,
    )
    ap.add_argument(
        "--ssl-verify",
        "--ssl_verify",
        help=(
            "Path to a CA_BUNDLE file with certificates of trusted CAs, "
            'this may be "False" to disable verification as per the '
            "requests API."
        ),
        type=_str_or_false,
        default=None,
        dest="ssl_verify",
    )
    ap.add_argument(
        "-k",
        "--insecure",
        help=(
            'Allow conda to perform "insecure" SSL connections and '
            "transfers. Equivalent to setting 'ssl_verify' to 'false'."
        ),
        action="store_false",
        dest="ssl_verify",
    )
    ap.add_argument(
        "--max-retries",
        help=(
            "Maximum  number of retries before a download error is reraised, "
            "defaults to 100"
        ),
        type=int,
        default=100,
        dest="max_retries",
    )
    ap.add_argument(
        "--no-progress",
        action="store_false",
        dest="show_progress",
        help="Do not display progress bars.",
    )
    return ap


def _init_logger(verbosity):
    # set up the logger
    global logger
    logger = logging.getLogger("conda_mirror")
    logmap = {0: logging.ERROR, 1: logging.WARNING, 2: logging.INFO, 3: logging.DEBUG}
    loglevel = logmap.get(verbosity, "3")

    # clear all handlers
    for handler in logger.handlers:
        logger.removeHandler(handler)
    logger.setLevel(loglevel)
    format_string = "%(levelname)s: %(message)s"
    formatter = logging.Formatter(fmt=format_string)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(loglevel)
    stream_handler.setFormatter(fmt=formatter)

    logger.addHandler(stream_handler)

    print(
        "Log level set to %s" % logging.getLevelName(logmap[verbosity]), file=sys.stdout
    )


def _parse_and_format_args():
    """
    Collect arguments from sys.argv and invoke the main() function.
    """
    parser = _make_arg_parser()
    args = parser.parse_args()
    # parse arguments without setting defaults
    given_args, _ = parser._parse_known_args(sys.argv[1:], argparse.Namespace())

    _init_logger(args.verbose)
    logger.debug("sys.argv: %s", sys.argv)

    if args.version:
        from . import __version__

        print(__version__)
        sys.exit(1)

    config_dict = {}
    if args.config:
        logger.info("Loading config from %s", args.config)
        with open(args.config, "r") as f:
            config_dict = yaml.safe_load(f)
        logger.info("config: %s", config_dict)

        # use values from config file unless explicitly given on command line
        for a in parser._actions:
            if (
                # value exists in config file
                (a.dest in config_dict)
                and
                # ignore values that can only be given on command line
                (a.dest not in {"config", "verbose", "version"})
                and
                # only use config file value if the value was not explicitly given on command line
                (a.dest not in given_args)
            ):
                logger.info("Using %s value from config file", a.dest)
                setattr(args, a.dest, config_dict.get(a.dest))

    blacklist = config_dict.get("blacklist")
    whitelist = config_dict.get("whitelist")

    for required in ("target_directory", "platform", "upstream_channel"):
        if not getattr(args, required):
            raise ValueError("Missing command line argument: %s", required)

    if args.pdb:
        # set the pdb_hook as the except hook for all exceptions
        def pdb_hook(exctype, value, traceback):
            pdb.post_mortem(traceback)

        sys.excepthook = pdb_hook

    proxies = args.proxy
    if proxies is not None:
        # use scheme of proxy url to generate dictionary
        # if no extra scheme is given
        # examples:
        # "http:https://user:pass@proxy.tld"
        #  -> {'http': 'https://user:pass@proxy.tld'}
        # "https://user:pass@proxy.tld"
        #  -> {'https': 'https://user:pass@proxy.tld'}
        scheme, *url = proxies.split(":")
        if len(url) > 1:
            url = ":".join(url)
        else:
            url = "{}:{}".format(scheme, url[0])
        proxies = {scheme: url}

    latest_dev = int(args.latest_dev)
    latest_non_dev = int(args.latest)

    # If --latest is specified, then --latest-dev are specified defaults to zero.
    if latest_dev < 0 and latest_non_dev >= 0:
        latest_dev = 0

    return {
        "upstream_channel": args.upstream_channel,
        "target_directory": args.target_directory,
        "temp_directory": args.temp_directory,
        "platform": args.platform,
        "num_threads": args.num_threads,
        "blacklist": blacklist,
        "whitelist": whitelist,
        "include_depends": args.include_depends,
        "latest_dev": latest_dev,
        "latest_non_dev": latest_non_dev,
        "dry_run": args.dry_run,
        "no_validate_target": args.no_validate_target,
        "minimum_free_space": args.minimum_free_space,
        "proxies": proxies,
        "ssl_verify": args.ssl_verify,
        "max_retries": args.max_retries,
        "show_progress": args.show_progress,
    }


def cli():
    """Thin wrapper around parsing the cli args and calling main with them"""
    main(**_parse_and_format_args())


def _remove_package(pkg_path, reason):
    """
    Log and remove a package.

    Parameters
    ----------
    pkg_path : str
        Path to a conda package that should be removed

    Returns
    -------
    pkg_path : str
        The full path to the package that is being removed
    reason : str
        The reason why the package is being removed
    """
    msg = "Removing: %s. Reason: %s" % (pkg_path, reason)
    if logger:
        logger.warning(msg)
    else:
        # Logging breaks in multiprocessing in Windows
        # TODO: Fix this properly with a logging Queue
        sys.stdout.write("Warning: " + msg)
    os.remove(pkg_path)
    return pkg_path, msg


def _validate(filename, md5=None, size=None):
    """Validate the conda package tarfile located at `filename` with any of the
    passed in options `md5` or `size. Also implicitly validate that
    the conda package is a valid tarfile.

    NOTE: Removes packages that fail validation

    Parameters
    ----------
    filename : str
        The path to the file you wish to validate
    md5 : str, optional
        If provided, perform an `md5sum` on `filename` and compare to `md5`
    size : int, optional
        if provided, stat the file at `filename` and make sure its size
        matches `size`

    Returns
    -------
    pkg_path : str
        The full path to the package that is being removed
    reason : str
        The reason why the package is being removed
    """
    if md5:
        calc = hashlib.md5(open(filename, "rb").read()).hexdigest()
        if calc == md5:
            # If the MD5 matches, skip the other checks
            return filename, None
        else:
            return _remove_package(
                filename,
                reason="Failed md5 validation. Expected: %s. Computed: %s"
                % (calc, md5),
            )

    if size and size != os.stat(filename).st_size:
        return _remove_package(filename, reason="Failed size test")

    try:
        with tarfile.open(filename) as t:
            t.extractfile("info/index.json").read().decode("utf-8")
    except (tarfile.TarError, EOFError):
        logger.info(
            "Validation failed because conda package is corrupted.", exc_info=True
        )
        return _remove_package(filename, reason="Tarfile read failure")

    return filename, None


def get_repodata(channel, platform, proxies=None, ssl_verify=None):
    """Get the repodata.json file for a channel/platform combo on anaconda.org

    Parameters
    ----------
    channel : str
        anaconda.org/CHANNEL
    platform : {'linux-64', 'linux-32', 'osx-64', 'win-32', 'win-64'}
        The platform of interest
    proxies : dict
        Proxys for connecting internet
    ssl_verify : str or bool
        Path to a CA_BUNDLE file or directory with certificates of trusted CAs

    Returns
    -------
    info : dict
    packages : dict
        keyed on package name (e.g., twisted-16.0.0-py35_0.tar.bz2)
    """
    url_template, channel = _maybe_split_channel(channel)
    url = url_template.format(
        channel=channel, platform=platform, file_name="repodata.json"
    )

    resp = requests.get(url, proxies=proxies, verify=ssl_verify).json()
    info = resp.get("info", {})
    packages = resp.get("packages", {})
    packages.update(resp.get("packages.conda", {}))
    # Patch the repodata.json so that all package info dicts contain a "subdir"
    # key.  Apparently some channels on anaconda.org do not contain the
    # 'subdir' field. I think this this might be relegated to the
    # Continuum-provided channels only, actually.
    for pkg_name, pkg_info in packages.items():
        pkg_info.setdefault("subdir", platform)
    return info, packages


def _download(
    url,
    target_directory,
    session: requests.Session,
    *,
    proxies=None,
    ssl_verify=None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    show_progress=False,
):
    """Download `url` to `target_directory`

    Parameters
    ----------
    url : str
        The url to download
    target_directory : str
        The path to a directory where `url` should be downloaded
    session: requests.Session
        HTTP session instance.
    proxies : dict
        Proxys for connecting internet
    ssl_verify : str or bool
        Path to a CA_BUNDLE file or directory with certificates of trusted CAs
    show_progress: bool
        Whether to display progress bars.

    Returns
    -------
    file_size: int
        The size in bytes of the file that was downloaded
    """
    file_size = 0
    logger.info("download_url=%s", url)
    # create a temporary file
    target_filename = url.split("/")[-1]
    download_filename = os.path.join(target_directory, target_filename)
    logger.debug("downloading to %s", download_filename)
    with open(download_filename, "w+b") as tf:
        ret = session.get(url, stream=True, proxies=proxies, verify=ssl_verify)
        size = int(ret.headers.get("Content-Length", 0))
        progress = tqdm(
            desc=target_filename,
            disable=(size < 1024) or not show_progress,
            total=size,
            leave=False,
            unit="byte",
            unit_scale=True,
        )
        for data in ret.iter_content(chunk_size):
            tf.write(data)
            progress.update(len(data))
        progress.close()
        file_size = os.path.getsize(download_filename)
    return file_size


def _download_backoff_retry(
    url,
    target_directory,
    session: requests.Session,
    *,
    proxies=None,
    ssl_verify=None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_retries: int = 100,
    show_progress=True,
):
    """Download `url` to `target_directory` with exponential backoff in the
    event of failure.

    Parameters
    ----------
    url : str
        The url to download
    target_directory : str
        The path to a directory where `url` should be downloaded
    session: requests.Session
        HTTP session instance.
    proxies : dict
        Proxys for connecting internet
    ssl_verify : str or bool
        Path to a CA_BUNDLE file or directory with certificates of trusted CAs
    chunk_size: int
        Size of contiguous chunk to download in bytes.
    max_retries : int, optional
        The maximum number of times to retry before the download error is reraised,
        default 100.
    show_progress: bool
        Whether to display progress bars.

    Returns
    -------
    file_size: int
        The size in bytes of the file that was downloaded
    """
    c = 0
    two_c = 1
    delay = 5.12e-5  # 51.2 us
    while c < max_retries:
        c += 1
        two_c *= 2
        try:
            rtn = _download(
                url,
                target_directory,
                session,
                proxies=proxies,
                ssl_verify=ssl_verify,
                chunk_size=chunk_size,
                show_progress=show_progress,
            )
            break
        except Exception:
            if c < max_retries:
                logger.debug(
                    "downloading failed, retrying {0}/{1}".format(c, max_retries)
                )
                time.sleep(delay * random.randint(0, two_c - 1))
            else:
                raise
    return rtn


def _list_conda_packages(local_dir):
    """List the conda packages (tar.bz2 or conda files) in `local_dir`

    Parameters
    ----------
    local_dir : str
        Some local directory with (hopefully) some conda packages in it

    Returns
    -------
    list
        List of conda packages in `local_dir`
    """
    contents = os.listdir(local_dir)
    return [fn for fn in contents if fn.endswith(".tar.bz2") or fn.endswith(".conda")]


def _validate_packages(package_repodata, package_directory, num_threads=1):
    """Validate local conda packages.

    NOTE1: This will remove any packages that are in `package_directory` that
           are not in `repodata` and also any packages that fail the package
           validation
    NOTE2: In concurrent mode (num_threads is not 1) this might be hard to kill
           using CTRL-C.

    Parameters
    ----------
    package_repodata : dict
        The contents of repodata.json
    package_directory : str
        Path to the local repo that contains conda packages
    num_threads : int
        Number of concurrent processes to use. Set to `0` to use a number of
        processes equal to the number of cores in the system. Defaults to `1`
        (i.e. serial package validation).

    Returns
    -------
    list
        Iterable of twoples of (pkg_path, reason) where
        pkg_path : str
            The full path to the package that is being removed
        reason : str
            The reason why the package is being removed
    """
    # validate local conda packages
    local_packages = _list_conda_packages(package_directory)

    # create argument list (necessary because multiprocessing.Pool.map does not
    # accept additional args to be passed to the mapped function)
    num_packages = len(local_packages)
    val_func_arg_list = [
        (package, num, num_packages, package_repodata, package_directory)
        for num, package in enumerate(sorted(local_packages))
    ]

    if num_threads == 1 or num_threads is None:
        # Do serial package validation (Takes a long time for large repos)
        validation_results = map(_validate_or_remove_package, val_func_arg_list)
    else:
        if num_threads == 0:
            num_threads = os.cpu_count()
            logger.debug(
                "num_threads=0 so it will be replaced by all available "
                "cores: %s" % num_threads
            )
        logger.info(
            "Will use {} threads for package validation." "".format(num_threads)
        )
        p = multiprocessing.Pool(num_threads)
        validation_results = p.map(_validate_or_remove_package, val_func_arg_list)
        p.close()
        p.join()

    return validation_results


def _validate_or_remove_package(args):
    """Validata or remove package.

    Parameters
    ----------
    args : tuple
        - `args[0]` is `package`.
        - `args[1]` is the number of the package in the list of all packages.
        - `args[2]` is the number of all packages.
        - `args[3]` is `package_repodata`.
        - `args[4]` is `package_directory`.

    Returns
    -------
    pkg_path : str
        The full path to the package that is being removed
    reason : str
        The reason why the package is being removed
    """
    # unpack arg tuple tuple
    package = args[0]
    num = args[1]
    num_packages = args[2]
    package_repodata = args[3]
    package_directory = args[4]

    # ensure the packages in this directory are in the upstream
    # repodata.json
    try:
        package_metadata = package_repodata[package]
    except KeyError:
        log_msg = f"{package} is not in the upstream index. Removing..."
        if logger:
            logger.warning(log_msg)
        else:
            # Windows does not handle multiprocessing logging well
            # TODO: Fix this properly with a logging Queue
            sys.stdout.write("Warning: " + log_msg)
        reason = "Package is not in the repodata index"
        package_path = os.path.join(package_directory, package)
        return _remove_package(package_path, reason=reason)
    # validate the integrity of the package, the size of the package and
    # its hashes
    log_msg = "Validating {:4d} of {:4d}: {}.".format(num + 1, num_packages, package)
    if logger:
        logger.info(log_msg)
    else:
        # Windows does not handle multiprocessing logging well
        # TODO: Fix this properly with a logging Queue
        sys.stdout.write("Info: " + log_msg)
    package_path = os.path.join(package_directory, package)
    return _validate(
        package_path, md5=package_metadata.get("md5"), size=package_metadata.get("size")
    )


def _find_non_recent_packages(
    packages: Dict[str, Dict[str, Any]],
    *,
    include: Iterable[str],
    latest_non_dev: int,
    latest_dev: int,
) -> Set[str]:
    """Computes set of package filenames that are not sufficiently recent

    Parameters
    ----------
    packages: packages dictionary from repodata.json
    include: package filenames to be considered
    latest_non_dev: number of non-dev packages that are sufficiently recent to be included
        if negative, then all non-dev packages will be included
    latest_dev: number of dev packages that are sufficnetly recent to be included
        if negative, then all dev packages will be included

    Returns
    -------
    non-recent packages: Set[str]
       Package filenames that are not sufficiently recent. This will be a subset of `include`
    """

    non_recent_packages: Set[str] = set()

    if latest_non_dev >= 0 or latest_dev >= 0:

        class PackageAndVersion(NamedTuple):
            package_file: str
            version: VersionOrder

        packages_by_name: Dict[str, List[PackageAndVersion]] = {}
        for key in include:
            metadata = packages[key]
            try:
                packages_by_name.setdefault(metadata["name"], []).append(
                    PackageAndVersion(key, VersionOrder(metadata["version"]))
                )
            except KeyError:
                pass  # ignore bad entries

        for curpackages in packages_by_name.values():
            curpackages.sort(
                key=lambda x: x.version, reverse=True
            )  # recent versions first
            dev_versions = [
                p.package_file for p in curpackages if "DEV" in p.version.version[-1]
            ]
            non_dev_versions = [
                p.package_file
                for p in curpackages
                if "DEV" not in p.version.version[-1]
            ]
            if latest_dev >= 0:
                non_recent_packages.update(dev_versions[latest_dev:])
            if latest_non_dev >= 0:
                non_recent_packages.update(non_dev_versions[latest_non_dev:])

    return non_recent_packages


def main(
    upstream_channel,
    target_directory,
    temp_directory,
    platform: Union[str, List[str]],
    blacklist=None,
    whitelist=None,
    include_depends=False,
    latest_non_dev: int = -1,
    latest_dev: int = -1,
    num_threads=1,
    dry_run=False,
    no_validate_target=False,
    minimum_free_space=0,
    proxies=None,
    ssl_verify=None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_retries=100,
    show_progress: bool = True,
):
    """

    Parameters
    ----------
    upstream_channel : str
        The anaconda.org channel that you want to mirror locally
        e.g., "conda-forge" or
        the defaults channel at "https://repo.continuum.io/pkgs/free"
    target_directory : str
        The path on disk to produce a local mirror of the upstream channel.
        Note that this is the directory that contains the platform
        subdirectories.
    temp_directory : str
        The path on disk to an existing and writable directory to temporarily
        store the packages before moving them to the target_directory to
        apply checks
    platform : Union[str,List[str]]
        The platforms that you wish to mirror. Common options are
        'linux-64', 'osx-64', 'win-64', 'win-32' and 'noarch'. Any platform is valid as
        long as the url resolves.
    blacklist : iterable of tuples, optional
        The values of blacklist should be (key, glob) where key is one of the
        keys in the repodata['packages'] dicts and glob is a thing to match
        on.  Note that all comparisons will be laundered through lowercasing.
    whitelist : iterable of tuples, optional
        The values of blacklist should be (key, glob) where key is one of the
        keys in the repodata['packages'] dicts and glob is a thing to match
        on.  Note that all comparisons will be laundered through lowercasing.
    include_depends: bool
        If true, then include packages matching dependencies of whitelisted
        packages as well.
    latest_dev: int
        If >= zero, then only that number of the most recent development versions of
        each package in a repo subdir will be downloaded.
    latest_non_dev: int
        If >= zero, then only that number of the most recent non development versions of
        each package in a repo subdir will be downloaded.
    num_threads : int, optional
        Number of threads to be used for concurrent validation.  Defaults to
        `num_threads=1` for non-concurrent mode.  To use all available cores,
        set `num_threads=0`.
    dry_run : bool, optional
        Defaults to False.
        If True, skip validation and exit after determining what needs to be
        downloaded and what needs to be removed.
    no_validate_target : bool, optional
        Defaults to False.
        If True, skip validation of files already present in target_directory.
    minimum_free_space : int, optional
        Stop downloading when free space target_directory or temp_directory reach this threshold.
    proxies : dict
        Proxys for connecting internet
    ssl_verify : str or bool
        Path to a CA_BUNDLE file or directory with certificates of trusted CAs
    chunk_size: int
        Size of chunks to download in bytes. Default is 1MB.
    max_retries : int, optional
        The maximum number of times to retry before the download error is reraised,
        default 100.
    show_progress: bool
        Show progress bar while downloading. True by default.

    Returns
    -------
    dict
        Summary of what was removed and what was downloaded.
        keys are:
        - validation : set of (path, reason) for each package that was validated.
                       packages where reason=None is a sentinel for a successful validation
        - download : set of (url, download_path) for each package that
                     was downloaded

    Notes
    -----
    the repodata['packages'] dictionary is formatted like this:

    keys are filenames, e.g.:
    tk-8.5.18-0.tar.bz2

    values are dictionaries, e.g.:
    {'arch': 'x86_64',
     'binstar': {'channel': 'main',
                 'owner_id': '55fc8527d3234d09d4951c71',
                 'package_id': '56380a159c73330b8ae858b8'},
     'build': '0',
     'build_number': 0,
     'date': '2015-03-16',
     # depends is the legacy key for old versions of conda
     'depends': [],
     'license': 'BSD-like',
     'license_family': 'BSD',
     'md5': '902f0fd689a01a835c9e69aefbe58fdd',
     'name': 'tk',
     'platform': 'linux',
     # requires is the new key that specifies the package requirements
     old versions of conda
     'requires': [],
     'size': 1960193,
     'version': '8.5.18'}
    """
    # TODO update these comments. They are no longer totally correct.
    # Steps:
    # 1. figure out blacklisted packages
    # 2. un-blacklist packages that are actually whitelisted
    # 3. remove blacklisted packages
    # 4. figure out final list of packages to mirror
    # 5. mirror new packages to temp dir
    # 6. validate new packages
    # 7. copy new packages to repo directory
    # 8. download repodata.json and repodata.json.bz2
    # 9. copy new repodata.json and repodata.json.bz2 into the repo
    summary = {
        "validating-existing": set(),
        "validating-new": set(),
        "downloaded": set(),
        "blacklisted": set(),
        "to-mirror": set(),
    }

    _platforms = [platform] if isinstance(platform, str) else platform

    for platform in _platforms:
        _download_platform(
            summary,
            upstream_channel,
            target_directory,
            temp_directory,
            platform,
            blacklist=blacklist,
            whitelist=whitelist,
            num_threads=num_threads,
            dry_run=dry_run,
            no_validate_target=no_validate_target,
            minimum_free_space=minimum_free_space,
            proxies=proxies,
            ssl_verify=ssl_verify,
            max_retries=max_retries,
            include_depends=include_depends,
            latest_non_dev=latest_non_dev,
            latest_dev=latest_dev,
            show_progress=show_progress,
        )

    return summary


def _download_platform(
    summary,
    upstream_channel,
    target_directory,
    temp_directory,
    platform: str,
    blacklist=None,
    whitelist=None,
    num_threads=1,
    dry_run=False,
    no_validate_target=False,
    minimum_free_space=0,
    proxies=None,
    ssl_verify=None,
    max_retries=100,
    include_depends=False,
    latest_non_dev: int = -1,
    latest_dev: int = -1,
    show_progress: bool = True,
):
    # Implementation:
    if not os.path.exists(os.path.join(target_directory, platform)):
        os.makedirs(os.path.join(target_directory, platform))

    info, packages = get_repodata(
        upstream_channel, platform, proxies=proxies, ssl_verify=ssl_verify
    )
    local_directory = os.path.join(target_directory, platform)

    # 1. validate local repo
    # validating all packages is taking many hours.
    # _validate_packages(repodata=repodata,
    #                    package_directory=local_directory,
    #                    num_threads=num_threads)

    # 2. figure out excluded packages
    excluded_packages: Set[str] = set()
    required_packages: Set[str] = set()
    # match blacklist conditions
    if blacklist:
        for blist in blacklist:
            logger.debug("exclude item: %s", blist)
            matched_packages = list(_match(packages, blist))
            logger.debug(pformat(matched_packages))
            excluded_packages.update(matched_packages)

    # 3. un-blacklist packages that are actually whitelisted
    # match whitelist on blacklist
    if whitelist:
        for wlist in whitelist:
            matched_packages = list(_match(packages, wlist))
            required_packages.update(matched_packages)
        excluded_packages.difference_update(required_packages)

    if include_depends:
        excluded_packages = _restore_required_dependencies(
            packages, excluded_packages, required_packages
        )

    # make final mirror list of not-blacklist + whitelist
    summary["blacklisted"].update(excluded_packages)

    logger.info("BLACKLISTED PACKAGES")
    logger.info(pformat(sorted(excluded_packages)))

    # Get a list of all packages in the local mirror
    if dry_run:
        local_packages = _list_conda_packages(local_directory)
        packages_slated_for_removal = [
            pkg_name
            for pkg_name in local_packages
            if pkg_name in summary["blacklisted"]
        ]
        logger.info("PACKAGES TO BE REMOVED")
        logger.info(pformat(sorted(packages_slated_for_removal)))

    possible_packages_to_mirror = set(packages.keys()) - excluded_packages

    # 3b remove non-latest packages if so specified.
    non_recent_packages = _find_non_recent_packages(
        packages,
        include=possible_packages_to_mirror,
        latest_non_dev=latest_non_dev,
        latest_dev=latest_dev,
    )
    possible_packages_to_mirror -= non_recent_packages

    # 4. Validate all local packages
    # construct the desired package repodata
    desired_repodata = {
        pkgname: packages[pkgname] for pkgname in possible_packages_to_mirror
    }
    if not (dry_run or no_validate_target):
        # Only validate if we're not doing a dry-run
        validation_results = _validate_packages(
            desired_repodata, local_directory, num_threads
        )
        summary["validating-existing"].update(validation_results)
    # 5. figure out final list of packages to mirror
    # do the set difference of what is local and what is in the final
    # mirror list
    local_packages = _list_conda_packages(local_directory)
    to_mirror = possible_packages_to_mirror - set(local_packages)
    logger.info("PACKAGES TO MIRROR")
    logger.info(pformat(sorted(to_mirror)))
    summary["to-mirror"].update(to_mirror)
    if dry_run:
        logger.info("Dry run complete. Exiting")
        return

    # 6. for each download:
    # a. download to temp file
    # b. validate contents of temp file
    # c. move to local repo
    # mirror all new packages
    total_bytes = 0
    minimum_free_space_kb = minimum_free_space * 1024 * 1024
    download_url, channel = _maybe_split_channel(upstream_channel)
    session = requests.Session()
    with tempfile.TemporaryDirectory(dir=temp_directory) as download_dir:
        logger.info("downloading to the tempdir %s", download_dir)
        for package_name in tqdm(
            sorted(to_mirror),
            desc=platform,
            unit="package",
            leave=False,
            disable=not show_progress,
        ):
            url = download_url.format(
                channel=channel, platform=platform, file_name=package_name
            )
            try:
                # make sure we have enough free disk space in the temp folder to meet threshold
                if shutil.disk_usage(download_dir).free < minimum_free_space_kb:
                    logger.error(
                        "Disk space below threshold in %s. Aborting download.",
                        download_dir,
                    )
                    break

                # download package
                total_bytes += _download_backoff_retry(
                    url,
                    download_dir,
                    session,
                    proxies=proxies,
                    ssl_verify=ssl_verify,
                    chunk_size=chunk_size,
                    max_retries=max_retries,
                    show_progress=show_progress,
                )

                # make sure we have enough free disk space in the target folder to meet threshold
                # while also being able to fit the packages we have already downloaded
                if (
                    shutil.disk_usage(local_directory).free - total_bytes
                ) < minimum_free_space_kb:
                    logger.error(
                        "Disk space below threshold in %s. Aborting download",
                        local_directory,
                    )
                    break

                summary["downloaded"].add((url, download_dir))
            except Exception as ex:
                logger.exception("Unexpected error: %s. Aborting download.", ex)
                break

        # validate all packages in the download directory
        validation_results = _validate_packages(
            packages, download_dir, num_threads=num_threads
        )
        summary["validating-new"].update(validation_results)
        logger.debug(
            "Newly downloaded files at %s are %s",
            download_dir,
            pformat(os.listdir(download_dir)),
        )

        # 8. Use already downloaded repodata.json contents but prune it of
        # packages we don't want
        repodata = {"info": info, "packages": packages}

        # compute the packages that we have locally
        packages_we_have = set(local_packages + _list_conda_packages(download_dir))
        # remake the packages dictionary with only the packages we have
        # locally
        repodata["packages"] = {
            name: info
            for name, info in repodata["packages"].items()
            if name in packages_we_have
        }
        _write_repodata(download_dir, repodata)

        # move new conda packages
        for f in _list_conda_packages(download_dir):
            old_path = os.path.join(download_dir, f)
            new_path = os.path.join(local_directory, f)
            logger.info("moving %s to %s", old_path, new_path)
            shutil.move(old_path, new_path)

        for f in ("repodata.json", "repodata.json.bz2"):
            download_path = os.path.join(download_dir, f)
            move_path = os.path.join(local_directory, f)
            shutil.move(download_path, move_path)

    # Also need to make a "noarch" channel or conda gets mad
    noarch_path = os.path.join(target_directory, "noarch")
    if not os.path.exists(noarch_path):
        os.makedirs(noarch_path, exist_ok=True)
        noarch_repodata = {"info": {}, "packages": {}}
        _write_repodata(noarch_path, noarch_repodata)


def _write_repodata(package_dir, repodata_dict):
    data = json.dumps(repodata_dict, indent=2, sort_keys=True)
    # strip trailing whitespace
    data = "\n".join(line.rstrip() for line in data.splitlines())
    # make sure we have newline at the end
    if not data.endswith("\n"):
        data += "\n"

    with open(os.path.join(package_dir, "repodata.json"), "w") as fo:
        fo.write(data)

    # compress repodata.json into the bz2 format. some conda commands still
    # need it
    bz2_path = os.path.join(package_dir, "repodata.json.bz2")
    with open(bz2_path, "wb") as fo:
        fo.write(bz2.compress(data.encode("utf-8")))


if __name__ == "__main__":
    cli()
