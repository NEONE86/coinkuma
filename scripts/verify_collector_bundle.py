"""Portable stdlib-only bundle integrity verifier (never imports ONE).

Accept legacy backslashes only as separators. Reject traversal, absolute paths,
drive names, Windows alternate streams and ambiguous aliases before file reads.
New exporters must emit Path.relative_to(root).as_posix(). Raw bytes are hashed
unchanged; LF Git exports have separate metadata from working-tree files.
"""
import argparse
import hashlib
import json
from pathlib import Path


def normalized_paths(root, names):
    root = Path(root).resolve()
    seen, result = set(), {}
    for name in names:
        if not isinstance(name, str):
            raise ValueError("non_string_manifest_path")
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if any(p in ("", ".", "..") or ":" in p or p.rstrip(" .") != p
               or any(ord(c) < 32 for c in p) for p in parts):
            raise ValueError("unsafe_manifest_path:" + name)
        key = normalized.casefold()
        if key in seen:
            raise ValueError("duplicate_normalized_path:" + name)
        seen.add(key)
        path = root.joinpath(*parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError("manifest_path_escape:" + name)
        result[name] = path
    return result


def verify(root, manifest):
    paths = normalized_paths(root, manifest)
    for name, path in paths.items():
        metadata = manifest[name]
        expected = metadata if isinstance(metadata, str) else metadata["sha256"]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("hash_mismatch:" + name)
        if isinstance(metadata, dict) and "size_bytes" in metadata and len(raw) != metadata["size_bytes"]:
            raise ValueError("size_mismatch:" + name)
    return dict(status="PASS", files=len(paths), raw_newlines_normalized=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--manifest", default="SHA256SUMS.json")
    args = p.parse_args()
    print(json.dumps(verify(args.root, json.loads((args.root / args.manifest).read_text(encoding="utf-8"))), indent=2))
