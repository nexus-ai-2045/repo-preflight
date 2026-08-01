from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SECTION_ALIASES = {
    "purpose": ("目的", "why", "overview", "概要"),
    "capabilities": ("できること", "what", "features", "機能"),
    "quickstart": ("クイックスタート", "quickstart", "quick start", "使い方", "usage"),
    "limits": ("制約", "限界", "limitations", "limits", "安全境界", "security", "注意"),
}
MAX_LINES = 300
MAX_SUMMARY_CHARS = 240


def _headings(lines: list[str]) -> list[tuple[int, str, int]]:
    result = []
    for number, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            result.append((len(match.group(1)), match.group(2).strip(), number))
    return result


def _first_summary(lines: list[str]) -> str:
    after_title = False
    paragraph: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not after_title:
            if line.startswith("# "):
                after_title = True
            continue
        if line.startswith("## "):
            break
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    return " ".join(paragraph)


def review(readme: Path) -> dict[str, object]:
    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = _headings(lines)
    findings: list[dict[str, object]] = []
    recommendations: set[str] = set()

    def add(
        code: str, message: str, *, severity: str = "error", line: int | None = None
    ) -> None:
        finding: dict[str, object] = {
            "code": code,
            "severity": severity,
            "message": message,
        }
        if line is not None:
            finding["line"] = line
        findings.append(finding)

    if not headings or headings[0][0] != 1:
        add("missing_h1", "先頭に製品名を示すH1が必要です。")
        recommendations.add("Template Creator")

    summary = _first_summary(lines)
    if not summary:
        add(
            "missing_lead_summary", "H1直後に対象読者と価値が分かる短い要約が必要です。"
        )
        recommendations.add("Product Design")
    elif len(summary) > MAX_SUMMARY_CHARS:
        add(
            "lead_summary_too_long",
            f"H1直後の要約を{MAX_SUMMARY_CHARS}文字以内に圧縮してください。",
        )

    normalized_headings = [title.casefold() for _, title, _ in headings]
    missing_sections = []
    for section, aliases in SECTION_ALIASES.items():
        if not any(
            any(alias in title for alias in aliases) for title in normalized_headings
        ):
            missing_sections.append(section)
            add(f"missing_{section}_section", f"READMEに{section}の節が必要です。")
    if missing_sections:
        recommendations.add("Template Creator")
    if "purpose" in missing_sections or "capabilities" in missing_sections:
        recommendations.add("Product Design")

    previous_level = 0
    for level, _, line_number in headings:
        if previous_level and level > previous_level + 1:
            add(
                "heading_level_jump",
                "見出しレベルを飛ばさないでください。",
                line=line_number,
            )
        previous_level = level

    if len(lines) > MAX_LINES:
        add(
            "readme_too_long",
            f"READMEは{MAX_LINES}行以内を目安にし、詳細をdocsへ分離してください。",
        )

    emoji_heading = re.compile(r"^#{1,6}\s+[^\w\s`#]", re.UNICODE)
    if any(emoji_heading.match(line) for line in lines):
        add(
            "decorative_heading_emoji",
            "見出し先頭の装飾絵文字は機能的な意味がない限り外してください。",
            severity="warning",
        )
        recommendations.add("ai-slop-check")

    generic_phrases = (
        "next-generation",
        "revolutionary",
        "cutting-edge",
        "comprehensive solution",
    )
    if any(phrase in text.casefold() for phrase in generic_phrases):
        add(
            "generic_marketing_copy",
            "抽象的な宣伝文句を具体的な利用価値へ置き換えてください。",
            severity="warning",
        )

    ordered_steps = len(re.findall(r"(?m)^\s*\d+[.)]\s+", text))
    has_mermaid = "```mermaid" in text.casefold()
    if ordered_steps >= 4 and not has_mermaid:
        recommendations.add("Visualize")

    has_image = bool(re.search(r"!\[[^]]*]\([^)]+\)", text))
    ui_pattern = re.compile(
        r"(?i)(?:\bui\b|画面|screenshots?|\binterface\b|\bdashboard\b|\bfrontend\b|\bweb app\b)"
    )
    ui_surface_declared = any(ui_pattern.search(title) for _, title, _ in headings)
    if ui_surface_declared and not has_image:
        recommendations.add("Figma or frontend-design")

    blocked = any(item["severity"] == "error" for item in findings)
    return {
        "schema": "public-readiness.readme-release-gate/v1",
        "status": "blocked" if blocked else "pass",
        "release_gate": "blocked_readme_design" if blocked else "passed_readme_design",
        "metrics": {
            "line_count": len(lines),
            "heading_count": len(headings),
            "lead_summary_chars": len(summary),
        },
        "findings": findings,
        "recommended_capabilities": sorted(recommendations),
        "human_visual_review_required": True,
        "external_actions_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="release前のREADME情報設計をread-only検査する"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    readme = args.repo.resolve() / "README.md"
    if not readme.is_file():
        result = {
            "schema": "public-readiness.readme-release-gate/v1",
            "status": "blocked",
            "release_gate": "blocked_readme_missing",
            "findings": [
                {
                    "code": "readme_missing",
                    "severity": "error",
                    "message": "README.mdがありません。",
                }
            ],
            "recommended_capabilities": ["Template Creator"],
            "human_visual_review_required": True,
            "external_actions_performed": False,
        }
    else:
        result = review(readme)
    print(
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.json
        else result["status"]
    )
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
