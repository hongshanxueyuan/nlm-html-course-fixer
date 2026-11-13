from __future__ import annotations

import argparse
import json
from pathlib import Path

from .production import AgentBridge, Studio, synthesize
from .repair_one import repair_one


def main():
    parser = argparse.ArgumentParser(description="Repair one HTML section in its existing Studio Gallery")
    parser.add_argument("--package", required=True)
    parser.add_argument("--studio-url", required=True)
    parser.add_argument("--section", required=True)
    parser.add_argument("--repair-dir", required=True)
    parser.add_argument("--tenant-config", required=True)
    parser.add_argument("--force-replace", action="store_true", help="Replace Gallery when its image bytes are known to differ despite matching filenames")
    args = parser.parse_args()
    studio = Studio(args.studio_url, args.tenant_config)
    bridge = AgentBridge(Path(args.repair_dir) / ".agent-bridge" / args.section)
    result = repair_one(args.package, args.studio_url, args.section, args.repair_dir,
                        split=bridge.split, review=bridge.review, synthesize=synthesize, studio=studio,
                        force_replace=args.force_replace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
