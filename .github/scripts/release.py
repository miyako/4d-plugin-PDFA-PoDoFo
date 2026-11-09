#!/usr/bin/env python3
"""Coordinated source versions and fail-closed, create-only publication."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys


VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def source_version(root):
    version = (root / "VERSION").read_text().removesuffix("\n")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must contain strict X.Y.Z (no leading zeros)")
    with (root / "PDFA/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleVersion") != version:
        raise ValueError("CFBundleVersion must match authoritative VERSION")
    return version


def bump(root, mode):
    version = source_version(root)
    parts = [int(part) for part in version.split(".")]
    index = {"major": 0, "minor": 1, "patch": 2}[mode]
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    new = ".".join(map(str, parts))
    path = root / "PDFA/Info.plist"
    text, count = re.subn(
        r"(<key>CFBundleVersion</key>\s*<string>)[^<]*(</string>)",
        lambda match: match[1] + new + match[2], path.read_text(),
    )
    if count != 1:
        raise ValueError("Expected exactly one CFBundleVersion")
    path.write_text(text)
    (root / "VERSION").write_text(new + "\n")
    return source_version(root)


def selected_checkout(root, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected an immutable 40-character commit SHA")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    if head != sha:
        raise ValueError("Checkout HEAD does not match the selected github.sha")


def check_available(repo, tag):
    # Establish repository access first: a hidden repository can also return 404.
    access = subprocess.run(
        ["gh", "api", f"repos/{repo}"], capture_output=True, text=True, check=False,
    )
    if access.returncode != 0:
        raise RuntimeError(f"Cannot establish repository access: {access.stderr}")
    for endpoint in (f"git/ref/tags/{tag}", f"releases/tags/{tag}"):
        result = subprocess.run(
            ["gh", "api", "--include", f"repos/{repo}/{endpoint}"],
            text=True, capture_output=True, check=False,
        )
        statuses = re.findall(r"^HTTP/\S+\s+([0-9]{3})\b", result.stdout, re.MULTILINE)
        status = statuses[-1] if statuses else None
        if result.returncode != 0 and status == "404":
            continue
        if result.returncode == 0 and status == "200":
            raise ValueError(f"{tag} already exists: {endpoint}")
        raise RuntimeError(f"Cannot establish availability of {tag}: "
                           f"{result.stdout}\n{result.stderr}")


def publish(root, repo, sha, assets):
    version = source_version(root)
    selected_checkout(root, sha)
    if not assets or any(not Path(asset).is_file() or Path(asset).stat().st_size == 0
                         for asset in assets):
        raise ValueError("All release assets must exist and be nonempty")
    tag = f"v{version}"
    check_available(repo, tag)
    try:
        subprocess.run(["git", "push", "--no-follow-tags", "origin",
                        f"{sha}:refs/tags/{tag}"], cwd=root, check=True)
    except subprocess.CalledProcessError:
        print(f"ERROR: Tag push failed for {tag}; remote state may be uncertain. "
              "Inspect it manually; no release was attempted.", file=sys.stderr)
        raise
    try:
        # gh release create can delete its temporary draft after an upload error.
        subprocess.run(["gh", "api", "--method", "POST", f"repos/{repo}/releases",
                        "--input", "-", "--silent"], cwd=root, check=True, text=True,
                       input=json.dumps({"tag_name": tag, "target_commitish": sha,
                                         "name": tag, "draft": False, "prerelease": False}))
        subprocess.run(["gh", "release", "upload", tag, "--repo", repo, *assets],
                       cwd=root, check=True)
    except subprocess.CalledProcessError:
        print(f"ERROR: {tag} was pushed, but release creation/upload failed. "
              "Inspect the remote tag/release and recover manually; "
              "nothing will be deleted or overwritten.", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["version", "bump", "available", "publish"])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--bump", choices=["patch", "minor", "major"])
    parser.add_argument("--sha")
    parser.add_argument("assets", nargs="*")
    args = parser.parse_args()
    version = source_version(args.root)
    if args.command == "bump":
        if not args.bump:
            parser.error("bump requires --bump")
        version = bump(args.root, args.bump)
    elif args.command in ("available", "publish"):
        repo = os.environ["GITHUB_REPOSITORY"]
        if args.command == "available":
            selected_checkout(args.root, args.sha or "")
            check_available(repo, f"v{version}")
        else:
            publish(args.root, repo, args.sha or "", args.assets)
    print(version)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write(f"version={version}\ntag=v{version}\n")


if __name__ == "__main__":
    main()
