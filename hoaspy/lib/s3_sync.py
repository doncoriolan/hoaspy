"""Mirror the collected dataset to S3.

Same shape as get-drone-sounds: the local files are the working copy, S3 is the
durable one, and a fresh checkout pulls state back down so it knows what has
already been collected rather than re-collecting it.

Credentials come from the standard AWS chain (`~/.aws/credentials`, env vars,
or an instance role). The identity needs `s3:GetObject`, `s3:PutObject` and
`s3:ListBucket` on the bucket/prefix — nothing wider.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import boto3
import botocore.exceptions

log = logging.getLogger("hoa.s3")

DEFAULT_PREFIX = "hoa-complaints/"

# Everything mirrored up at the end of a run.
SYNCED_FILES = [
    "posts.jsonl",
    "comments.jsonl",
    "comment_posts.jsonl",
    "complaints.csv",
    "firms.csv",
    "skipped.json",
    "run_log.json",
]

# Pulled down before a run when missing locally: without these, a fresh
# checkout would re-collect everything and duplicate the corpus.
RESUME_FILES = ["posts.jsonl", "comments.jsonl", "comment_posts.jsonl", "skipped.json"]

CONTENT_TYPES = {
    ".jsonl": "application/x-ndjson",
    ".json": "application/json",
    ".csv": "text/csv",
}

# Records the remote version we last saw, so a push can tell "nobody else
# touched this" from "another machine wrote here since we pulled".
MARKER = ".s3_sync.json"


class S3Error(RuntimeError):
    pass


def is_directory_bucket(bucket: str) -> bool:
    """S3 Express One Zone buckets are named `<base>--<azid>--x-s3`.

    They behave differently in two ways that matter here: they have no public
    access controls at all (private by construction, so nothing to check), and
    they are single-AZ — durable against disk failure, not against losing the
    availability zone.
    """
    return bucket.endswith("--x-s3")


def resolve_target(cfg: dict) -> tuple[str | None, str]:
    """(bucket, prefix). The S3_BUCKET env var wins over config.yml."""
    s3cfg = cfg.get("s3") or {}
    bucket = os.getenv("S3_BUCKET") or s3cfg.get("bucket") or None
    prefix = s3cfg.get("prefix", DEFAULT_PREFIX)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def enabled(cfg: dict) -> bool:
    s3cfg = cfg.get("s3") or {}
    bucket, _ = resolve_target(cfg)
    return bool(s3cfg.get("enabled", True) and bucket)


from hoaspy import ROOT

ENV_FILE = ROOT / ".env"


def env_credentials(path: Path = ENV_FILE) -> dict:
    """Fallback credentials from the repo's git-ignored `.env`
    (`ACCESSKEYID=` / `SECRETACCESSKEY=` / optional `REGION=`), used only when
    the standard boto3 chain has nothing. Values are never logged."""
    if not path.exists():
        return {}
    kv: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip().upper()] = v.strip().strip("'\"")
    out = {}
    if kv.get("ACCESSKEYID") and kv.get("SECRETACCESSKEY"):
        out = {"aws_access_key_id": kv["ACCESSKEYID"],
               "aws_secret_access_key": kv["SECRETACCESSKEY"]}
        if kv.get("REGION"):
            out["region_name"] = kv["REGION"]
    return out


def get_client(cfg: dict | None = None):
    s3cfg = (cfg or {}).get("s3") or {}
    region = s3cfg.get("region") or os.getenv("AWS_REGION")
    kwargs = {"region_name": region} if region else {}
    if boto3.session.Session().get_credentials() is None:
        kwargs = {**env_credentials(), **kwargs}
    if is_directory_bucket(resolve_target(cfg or {})[0] or "") and "region_name" not in kwargs:
        # `<base>--use1-az4--x-s3` encodes its region; directory buckets
        # must be addressed from it.
        m = re.search(r"--([a-z]+)(\d)-az\d--x-s3$", resolve_target(cfg or {})[0])
        if m:
            kwargs["region_name"] = {"use": "us-east-", "usw": "us-west-", "euw": "eu-west-",
                                     "euc": "eu-central-", "apne": "ap-northeast-",
                                     "aps": "ap-south-", "apse": "ap-southeast-"}.get(
                                         m.group(1), "") + m.group(2) or None
            if not kwargs["region_name"]:
                del kwargs["region_name"]
    return boto3.client("s3", **kwargs)


def _load_marker(data_dir: Path) -> dict:
    path = data_dir / MARKER
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save_marker(data_dir: Path, marker: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True))


def _remote_version(s3, bucket: str, key: str) -> str | None:
    """ETag of the object, or None when it doesn't exist."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ETag"]
    except botocore.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
            return None
        raise


def check_access(s3, bucket: str, prefix: str) -> list[str]:
    """Verify the bucket is reachable. Returns a list of warnings."""
    warnings: list[str] = []
    try:
        s3.head_bucket(Bucket=bucket)
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            raise S3Error(f"bucket {bucket!r} does not exist") from exc
        if code in ("403", "AccessDenied"):
            raise S3Error(
                f"access denied to bucket {bucket!r} — check the credential's "
                "policy covers s3:ListBucket on it"
            ) from exc
        raise S3Error(f"could not reach bucket {bucket!r}: {exc}") from exc
    except botocore.exceptions.NoCredentialsError as exc:
        raise S3Error(
            "no AWS credentials found. Configure ~/.aws/credentials, or set "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        ) from exc

    if is_directory_bucket(bucket):
        # No ACLs and no public access to check — GetPublicAccessBlock returns
        # MethodNotAllowed. Flag the durability trade-off instead.
        warnings.append(
            f"{bucket} is a directory bucket (S3 Express One Zone): single-AZ "
            "storage, no versioning. Fine as a working copy; keep an archival "
            "copy elsewhere if this corpus is the record of the study."
        )
        return warnings

    # This dataset keeps permalinks, so a public bucket would republish
    # identifiable posts. Worth one API call to notice.
    try:
        cfg = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        if not all(cfg.get(k) for k in ("BlockPublicAcls", "BlockPublicPolicy",
                                        "IgnorePublicAcls", "RestrictPublicBuckets")):
            warnings.append(
                f"bucket {bucket!r} does not have full Block Public Access enabled — "
                "this dataset keeps permalinks, so it should not be public"
            )
    except botocore.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchPublicAccessBlockConfiguration":
            warnings.append(
                f"bucket {bucket!r} has no Block Public Access configuration at all — "
                "verify it is private before pushing"
            )
        # AccessDenied here just means we can't check; not fatal.
    return warnings


def pull(s3, bucket: str, prefix: str, data_dir: Path,
         files: list[str] | None = None, force: bool = False) -> list[str]:
    """Download state files that are missing locally (or all, with force)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = _load_marker(data_dir)
    pulled: list[str] = []

    for name in files or RESUME_FILES:
        local = data_dir / name
        if local.exists() and not force:
            continue
        key = f"{prefix}{name}"
        try:
            s3.download_file(bucket, key, str(local))
        except botocore.exceptions.ClientError:
            continue  # not in S3 yet — normal on a first run
        marker[name] = _remote_version(s3, bucket, key)
        pulled.append(name)
        log.info("pulled s3://%s/%s", bucket, key)

    if pulled:
        _save_marker(data_dir, marker)
    return pulled


def push(s3, bucket: str, prefix: str, data_dir: Path,
         sse: str | None = "AES256", force: bool = False,
         files: list[str] | None = None) -> list[str]:
    """Upload the local state files.

    Refuses to overwrite an object that changed remotely since we last pulled or
    pushed it — that means another machine wrote there, and a blind upload would
    silently discard its records. `force` overrides.
    """
    marker = _load_marker(data_dir)
    conflicts: list[str] = []
    pushed: list[str] = []

    for name in files or SYNCED_FILES:
        local = data_dir / name
        if not local.exists():
            continue
        key = f"{prefix}{name}"
        remote = _remote_version(s3, bucket, key)
        if remote is not None and marker.get(name) not in (None, remote) and not force:
            conflicts.append(name)
            continue

        extra = {"ContentType": CONTENT_TYPES.get(local.suffix, "application/octet-stream")}
        if sse:
            extra["ServerSideEncryption"] = sse
        s3.upload_file(str(local), bucket, key, ExtraArgs=extra)
        marker[name] = _remote_version(s3, bucket, key)
        pushed.append(name)
        log.info("pushed %s -> s3://%s/%s (%.1f KB)",
                 name, bucket, key, local.stat().st_size / 1024)

    _save_marker(data_dir, marker)

    if conflicts:
        raise S3Error(
            f"{', '.join(conflicts)} changed in S3 since this machine last synced — "
            "another run wrote there. Pull to a scratch directory and merge, or "
            "re-push with --force to overwrite it."
        )
    return pushed


def main(argv: list[str] | None = None) -> int:
    """Push (or pull) an explicit list of files under a prefix:

        ./venv/bin/python -m hoaspy.lib.s3_sync push --prefix hoa-records/ --dir records \\
            state_corps.jsonl state_registries.jsonl irs_exempt_orgs.jsonl

    Refuses anything that looks like the member database (reviews.db) or a
    cookie file — those never leave the machine."""
    import argparse
    import yaml

    ap = argparse.ArgumentParser(description=main.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["push", "pull"])
    ap.add_argument("files", nargs="+", help="file names relative to --dir")
    ap.add_argument("--dir", type=Path, default=Path("."), help="local directory")
    ap.add_argument("--prefix", required=True, help="S3 key prefix, e.g. hoa-records/")
    ap.add_argument("--config", type=Path, default=ROOT / "config.yml")
    ap.add_argument("--force", action="store_true", help="overwrite a remote conflict")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    forbidden = [f for f in args.files if f.endswith(".db") or "cookie" in f.lower()]
    if forbidden:
        print(f"refusing to sync {', '.join(forbidden)}: member data / credentials stay local",
              file=sys.stderr)
        return 2
    cfg = yaml.safe_load(args.config.read_text()) if args.config.exists() else {}
    bucket, _ = resolve_target(cfg)
    if not bucket:
        print("no bucket configured (config.yml s3.bucket or S3_BUCKET)", file=sys.stderr)
        return 2
    prefix = args.prefix if args.prefix.endswith("/") else args.prefix + "/"
    try:
        s3 = get_client(cfg)
        for warning in check_access(s3, bucket, prefix):
            log.warning(warning)
        if args.action == "push":
            done = push(s3, bucket, prefix, args.dir, sse=(cfg.get("s3") or {}).get("sse", "AES256"),
                        force=args.force, files=args.files)
        else:
            done = pull(s3, bucket, prefix, args.dir, files=args.files, force=args.force)
    except S3Error as exc:
        print(f"S3: {exc}", file=sys.stderr)
        return 1
    print(f"{args.action}ed {len(done)} file(s) {'to' if args.action == 'push' else 'from'} "
          f"s3://{bucket}/{prefix}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
