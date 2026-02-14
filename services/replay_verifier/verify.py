import json, sys, os
from services.replay_verifier.hashutil import sha256_tree


def fail(msg):
    print("REPLAY_FAIL:", msg, flush=True)
    sys.exit(3)


def load(p):
    with open(p, "r") as f:
        return json.load(f)


def main(old_bundle, new_bundle):
    old = load(os.path.join(old_bundle, "manifest.json"))
    new = load(os.path.join(new_bundle, "manifest.json"))

    if old["deps"] != new["deps"]:
        fail("deps mismatch")

    if old["env"] != new["env"]:
        fail("env mismatch")

    if old["patch_hash"] != new["patch_hash"]:
        fail("patch mismatch")

    if old["kernel_trace"] != new["kernel_trace"]:
        fail("kernel mismatch")

    print("REPLAY_OK")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
