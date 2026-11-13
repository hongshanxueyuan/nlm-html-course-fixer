"""Run selected HTML repairs, isolating each section's failures."""
from __future__ import annotations

from pathlib import Path

from .repair_one import _json, _write, repair_one


def _first_chapter(package, repair_dir):
    first = set()
    for path in [*package.rglob("course-content.json"), *package.rglob("fira_course-*.json")]:
        if path.resolve().is_relative_to(repair_dir):
            continue
        source = _json(path)
        chapters = source.get("chapters", [])
        # FIRA course exports include a pre-course "训战启程" chapter.
        lessons = [chapter for chapter in chapters if chapter.get("name") != "训战启程"]
        if lessons and source.get("course_id") and lessons[0].get("name"):
            first.add((source["course_id"], lessons[0]["name"]))
    if len(first) != 1:
        raise ValueError("first source chapter is missing or ambiguous")
    return next(iter(first))[1]


def _section_chapter(package, repair_dir, title):
    matches = set()
    for path in [*package.rglob("course-content.json"), *package.rglob("fira_course-*.json")]:
        if path.resolve().is_relative_to(repair_dir):
            continue
        source = _json(path)
        for chapter in source.get("chapters", []):
            for section in chapter.get("sections", []):
                if section.get("name") == title:
                    matches.add((source.get("course_id"), chapter.get("name")))
    if len(matches) != 1 or not all(next(iter(matches))):
        raise ValueError("source section chapter is missing or ambiguous")
    return next(iter(matches))[1]


def run_batch(package, target_url, repair_dir, *, studio, synthesize, split=None, review=None,
              sections=None, chapter=None, first_chapter=False, force_replace=False, agent_factory=None):
    package, repair_dir = Path(package).resolve(), Path(repair_dir).resolve()
    if sum((chapter is not None, sections is not None, first_chapter)) > 1:
        raise ValueError("choose one scope")
    if not repair_dir.is_relative_to(package) or repair_dir == package:
        raise ValueError("repair directory must be a dedicated child of the course package")
    marker = repair_dir / ".nlm-html-course-fixer"
    if repair_dir.exists() and not marker.is_file() and any(repair_dir.iterdir()):
        raise ValueError("repair directory is not dedicated to this skill")
    repair_dir.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    if first_chapter:
        chapter = _first_chapter(package, repair_dir)
    if agent_factory is None and (split is None or review is None):
        raise ValueError("split and review boundaries are required")
    ids, titles = [], {}
    for path in package.rglob("section-list.json"):
        if path.resolve().is_relative_to(repair_dir):
            continue
        for item in _json(path).get("sections", []):
            if item.get("block_type") == "html" and item.get("section_id") not in ids:
                ids.append(item.get("section_id"))
                titles[item.get("section_id")] = item.get("section_title")
    selected = list(sections) if sections is not None else ids
    if any(not isinstance(section_id, str) or not section_id for section_id in selected) or len(set(selected)) != len(selected):
        raise ValueError("section identifiers must be nonempty and unique")
    results = []
    for section_id in selected:
        try:
            if section_id not in ids:
                raise ValueError("section is not an HTML entry in the source manifest")
            if chapter is not None and _section_chapter(package, repair_dir, titles[section_id]) != chapter:
                continue
            agent = agent_factory(section_id) if agent_factory else None
            result = repair_one(package, target_url, section_id, repair_dir, split=agent.split if agent else split,
                                review=agent.review if agent else review, synthesize=synthesize, studio=studio,
                                force_replace=force_replace)
        except Exception as exc:
            result = {"section_id": section_id, "target_url": target_url, "status": "failed",
                      "phase": "inputs", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                      "recovery": "not needed"}
        results.append(result)
    scope = {"sections": list(sections)} if sections is not None else ({"first_chapter": chapter} if first_chapter else ({"chapter": chapter} if chapter is not None else {"course": True}))
    if not results:
        results.append({"status": "failed", "phase": "selection", "target_url": target_url,
                        "reason": "no HTML sections matched the selected scope", "recovery": "not needed"})
    statuses = {result["status"] for result in results}
    status = "unconfirmed" if "unconfirmed" in statuses else ("failed" if "failed" in statuses else "success")
    report = {"target_url": target_url, "source_package": str(package), "scope": scope, "status": status, "sections": results}
    _write(repair_dir / "batch-report.json", report)
    return report
