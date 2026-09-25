#!/usr/bin/env python3
#
# Copyright 2026 Enrico Bregolin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Source fetcher — puts the source's published file on disk, ready for read_sheet.py.

Exists for the same reason as read_sheet.py: nothing the source publishes may pass
through a language model on its way to the arithmetic. The agent runs this; the bytes
travel over git and land in a file. The model never sees them and never retypes them.

Two kinds of source, both declared in the reader's own config:

  {"signal_source": {"type": "local", "path": "/path/to/book.xlsx"}}

  {"signal_source": {"type": "git",
                     "repo": "https://github.com/<owner>/<repo>.git",
                     "path": "book.xlsx.enc",
                     "key":  "<the key the source gave you>"}}

The git form exists because an automated check runs with no computer attached: it
cannot reach a synced folder, and its sandbox reaches very little of the network.
Git over https is one of the few channels open, which makes it the transport rather
than a preference. Note that only PUBLIC repositories are reachable from such a
sandbox — which is exactly why the published file is encrypted rather than the
repository being private. The key is what the source hands to its readers.

When "key" is present the file is decrypted after download. The format is what
`openssl enc -aes-256-cbc -pbkdf2 -iter 480000 -salt` produces: an 8-byte salt
behind a "Salted__" marker, key and IV derived with PBKDF2-SHA256. Decryption is
done here in Python rather than by shelling out, so it does not depend on which
openssl variant the machine happens to ship.

Usage:
  python3 fetch_source.py --config ~/order-sizer/config.json --out /tmp/order-sizer/source.xlsx

  # overrides, for testing a source before writing it into the config
  python3 fetch_source.py --repo <url> --path <file> [--key-file <f>] --out <f>
  python3 fetch_source.py --local <file> --out <f>

Prints one JSON object on stdout, and exits non-zero on any failure: a check that
cannot prove which bytes it read must stop rather than guess.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile


def die(msg):
    print(json.dumps({"error": msg}))
    sys.exit(1)


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        die("%s failed: %s" % (cmd[0], (p.stderr or p.stdout).strip()[:400]))
    return p.stdout.strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def decrypt(src, dst, key):
    """AES-256-CBC in OpenSSL's salted container, PBKDF2-SHA256 480000 iterations."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        backend = "cryptography"
    except ImportError:
        backend = "openssl"

    with open(src, "rb") as f:
        blob = f.read()
    if len(blob) < 32 or blob[:8] != b"Salted__":
        die("the published file is not in the expected encrypted format — "
            "check that the configured path points at the encrypted file")

    if backend == "openssl":
        # No third-party crypto available: fall back to the openssl binary, which
        # produced this container in the first place.
        if shutil.which("openssl") is None:
            die("neither the cryptography package nor the openssl command is available; "
                "one of the two is needed to decrypt the source")
        kf = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            os.chmod(kf.name, 0o600)
            kf.write(key)
            kf.close()
            p = subprocess.run(
                ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "480000",
                 "-in", src, "-out", dst, "-pass", "file:%s" % kf.name],
                capture_output=True, text=True)
        finally:
            os.unlink(kf.name)
        if p.returncode != 0:
            die("could not decrypt the published file: either the key is "
                "wrong or the file is damaged")
        return

    salt = blob[8:16]
    ki = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, 480000, 48)
    c = Cipher(algorithms.AES(ki[:32]), modes.CBC(ki[32:48])).decryptor()
    plain = c.update(blob[16:]) + c.finalize()
    pad = plain[-1] if plain else 0
    # A wrong key and a damaged file are indistinguishable here: both surface as
    # padding that makes no sense. Say both, rather than sending the reader to
    # check the wrong one.
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        die("could not decrypt the published file: either the key is "
            "wrong or the file is damaged")
    with open(dst, "wb") as f:
        f.write(plain[:-pad])


def from_git(repo, rel, out, key):
    tmp = tempfile.mkdtemp(prefix="source-")
    try:
        run(["git", "clone", "--quiet", "--depth", "1", repo, tmp])
        src = os.path.join(tmp, rel)
        if not os.path.isfile(src):
            listing = []
            for root, _, files in os.walk(tmp):
                if ".git" in root:
                    continue
                for f in files:
                    listing.append(os.path.relpath(os.path.join(root, f), tmp))
            die("'%s' is not in the published repository. Present: %s"
                % (rel, ", ".join(sorted(listing)[:20]) or "(empty)"))
        if key:
            decrypt(src, out, key)
        else:
            shutil.copyfile(src, out)
        return {
            "origin": "git", "repo": repo, "encrypted": bool(key),
            "commit": run(["git", "log", "-1", "--format=%H"], cwd=tmp),
            "published_at": run(["git", "log", "-1", "--format=%cI"], cwd=tmp),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="reader config holding signal_source")
    ap.add_argument("--repo")
    ap.add_argument("--path")
    ap.add_argument("--key-file", help="file holding the source's key")
    ap.add_argument("--local", help="take an existing local file instead")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    src = None
    if a.config:
        try:
            with open(os.path.expanduser(a.config)) as f:
                cfg = json.load(f)
        except Exception as e:
            die("could not read the configuration: %s" % e)
        src = cfg.get("signal_source")
        # Configurations written before remote sources existed carry a plain path.
        if not src and cfg.get("signal_file"):
            src = {"type": "local", "path": cfg["signal_file"]}
        if not src:
            die("the configuration declares no source — set signal_source")

    if a.local:
        src = {"type": "local", "path": a.local}
    elif a.repo:
        key = None
        if a.key_file:
            with open(a.key_file) as f:
                key = f.read().strip()
        src = {"type": "git", "repo": a.repo, "path": a.path, "key": key}

    if not src:
        die("nothing to fetch: pass --config, --repo or --local")

    kind = src.get("type", "local")
    if kind == "local":
        p = os.path.expanduser(src.get("path") or "")
        if not os.path.isfile(p):
            die("the configured source file does not exist: %s. Do not guess a "
                "replacement — say so and offer to set a new one." % p)
        shutil.copyfile(p, out)
        info = {"origin": "local", "path": p, "encrypted": False}
    elif kind == "git":
        if not (src.get("repo") and src.get("path")):
            die("the git source needs both 'repo' and 'path'")
        info = from_git(src["repo"], src["path"], out, src.get("key"))
    else:
        die("unknown source type '%s' — use 'local' or 'git'" % kind)

    info.update({"file": out, "sha256": sha256(out), "bytes": os.path.getsize(out)})
    print(json.dumps(info))


if __name__ == "__main__":
    main()
