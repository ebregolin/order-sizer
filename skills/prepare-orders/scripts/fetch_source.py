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

A source may give every reader the same key, or a different key to each. Nothing in
the reader's config says which, and nothing needs to: the published repository says
it. If a file named "<path>.keys.json" sits beside the encrypted file, the source
uses one envelope per reader — the file's own key, sealed separately for each of
them. The reader's key then opens their envelope, and what is inside opens the file.
If there is no such file, or the reader's key matches no envelope, the key is tried
on the file directly, which is the single-key arrangement. Either way the reader
holds one key and configures the same three things.

Usage:
  python3 fetch_source.py --config ~/order-sizer/config.json --out /tmp/order-sizer/source.xlsx

  # overrides, for testing a source before writing it into the config
  python3 fetch_source.py --repo <url> --path <file> [--key-file <f>] --out <f>
  python3 fetch_source.py --local <file> --out <f>

Prints one JSON object on stdout, and exits non-zero on any failure: a check that
cannot prove which bytes it read must stop rather than guess.
"""
import argparse, base64, hashlib, json, os, shutil, subprocess, sys, tempfile


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


class Undecryptable(Exception):
    pass


def _aes_cbc_decrypt(key, iv, data):
    """AES-256-CBC, PKCS#7. Raises Undecryptable when the result makes no sense."""
    if not data or len(data) % 16:
        raise Undecryptable()
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        # No third-party crypto available: fall back to the openssl binary. The key
        # is already derived at this point, so it is passed as raw hex.
        if shutil.which("openssl") is None:
            die("neither the cryptography package nor the openssl command is available; "
                "one of the two is needed to decrypt the source")
        p = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex()],
            input=data, capture_output=True)
        if p.returncode != 0:
            raise Undecryptable()
        return p.stdout
    c = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = c.update(data) + c.finalize()
    pad = plain[-1] if plain else 0
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise Undecryptable()
    return plain[:-pad]


def decrypt(src, dst, key):
    """AES-256-CBC in OpenSSL's salted container, PBKDF2-SHA256 480000 iterations."""
    with open(src, "rb") as f:
        blob = f.read()
    if len(blob) < 32 or blob[:8] != b"Salted__":
        die("the published file is not in the expected encrypted format — "
            "check that the configured path points at the encrypted file")
    ki = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), blob[8:16], 480000, 48)
    plain = _aes_cbc_decrypt(ki[:32], ki[32:48], blob[16:])
    with open(dst, "wb") as f:
        f.write(plain)


def open_envelope(keys_path, key):
    """Return the file's key if this reader's key opens one of the published
    envelopes, else None. Never raises on a key that simply is not listed: that is
    the normal case for a source still handing the same key to everyone."""
    try:
        with open(keys_path) as f:
            doc = json.load(f)
        if doc.get("format") != "envelopes-1":
            die("the source publishes its keys in a format this version does not "
                "know (%s) — update the plugin" % doc.get("format"))
        salt = base64.b64decode(doc["salt"])
        d = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt,
                                int(doc["iterations"]), 48)
        sealed = doc["envelopes"].get(d[:16].hex())
    except (ValueError, KeyError, TypeError):
        die("the source's key file is damaged — nothing can be read until the "
            "source publishes again")
    if sealed is None:
        return None
    try:
        raw = base64.b64decode(sealed)
        inner = _aes_cbc_decrypt(d[16:48], raw[:16], raw[16:]).decode("utf-8")
    except (Undecryptable, ValueError):
        return None
    return inner[4:] if inner.startswith("ck1:") else None


def unlock(src, out, key):
    """Decrypt src with the reader's key, whichever arrangement the source uses.
    Returns "personal" or "shared"."""
    keys_path = src + ".keys.json"
    has_envelopes = os.path.isfile(keys_path)
    if has_envelopes:
        content_key = open_envelope(keys_path, key)
        if content_key is not None:
            try:
                decrypt(src, out, content_key)
            except Undecryptable:
                die("your key was accepted but the published file did not open — "
                    "the file is damaged; nothing can be read until the source "
                    "publishes again")
            return "personal"
    try:
        decrypt(src, out, key)
    except Undecryptable:
        if has_envelopes:
            die("could not open the published file: this key is not among the ones "
                "the source currently accepts. Either it was mistyped, or the source "
                "has replaced or withdrawn it. Nothing was read — do not treat this "
                "as 'no new positions'.")
        # A wrong key and a damaged file are indistinguishable here: both surface as
        # padding that makes no sense. Say both, rather than sending the reader to
        # check the wrong one.
        die("could not decrypt the published file: either the key is "
            "wrong or the file is damaged")
    return "shared"


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
        access = None
        if key:
            access = unlock(src, out, key)
        else:
            shutil.copyfile(src, out)
        return {
            "origin": "git", "repo": repo, "encrypted": bool(key), "access": access,
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
