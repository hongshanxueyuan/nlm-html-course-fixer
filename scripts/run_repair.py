from __future__ import annotations

import argparse
import json
from pathlib import Path

from .production import AgentBridge, Studio, find_tenant_config, synthesize
from .batch_repair import run_batch


def main():
    parser = argparse.ArgumentParser(description="Repair selected HTML sections in a Studio course")
    parser.add_argument("--package", required=True)
    parser.add_argument("--studio-url", required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--section", action="append", help="Section identifier; repeat for multiple sections")
    scope.add_argument("--chapter", help="Exact source chapter name")
    scope.add_argument("--first-chapter", action="store_true", help="Select only the first source chapter")
    parser.add_argument("--repair-dir", help="Dedicated directory inside the course package")
    parser.add_argument("--tenant-config", help="Optional Studio tenant configuration JSON")
    parser.add_argument("--force-replace", action="store_true", help="Replace Gallery when its image bytes are known to differ despite matching filenames")
    args = parser.parse_args()
    repair_dir = Path(args.repair_dir) if args.repair_dir else Path(args.package) / "nlm-html-course-fixer"
    tenant_config = find_tenant_config(args.studio_url, args.package, repair_dir, args.tenant_config)
    studio = Studio(args.studio_url, tenant_config)
    result = run_batch(args.package, args.studio_url, repair_dir, sections=args.section,
                       chapter=args.chapter, first_chapter=args.first_chapter, studio=studio,
                       agent_factory=lambda section: AgentBridge(repair_dir / ".agent-bridge" / section),
                       synthesize=synthesize, force_replace=args.force_replace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
