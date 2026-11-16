"""Initialize a new project for any supported language.

Usage:
    python init_project.py <language> [project_path]

Examples:
    python init_project.py python my-python-app
    python init_project.py go ./my-go-project
    python init_project.py rust C:\\projects\\my-rust-app

List available languages:
    python init_project.py --list
"""
import argparse
import os
import sys
from pathlib import Path

# Allow running this script directly from the scripts/ folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.language_templates import get_template, list_templates


def scaffold_project(template_key: str, project_path: Path, overwrite: bool = False) -> bool:
    """Create scaffold files for the given language template."""
    template = get_template(template_key)
    if template is None:
        print(f"❌ Unknown language: {template_key}")
        available = ", ".join(sorted(list_templates().keys()))
        print(f"   Available: {available}")
        return False

    # Create project directory if it doesn't exist
    project_path.mkdir(parents=True, exist_ok=True)

    if not overwrite:
        existing = [f for f in template.scaffold if (project_path / f).exists()]
        if existing:
            print(f"⚠️ Some files already exist: {', '.join(existing)}")
            print("   Use --overwrite to replace them.")
            return False

    created = []
    skipped = []
    for rel_path, content in template.scaffold.items():
        file_path = project_path / rel_path
        # Ensure parent directory exists (e.g., src/main.rs)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists() and not overwrite:
            skipped.append(rel_path)
            continue

        file_path.write_text(content, encoding="utf-8")
        created.append(rel_path)

    # Print summary
    emoji = "🐍" if template_key == "python" else \
            "🟢" if template_key in ("nodejs", "typescript") else \
            "☕" if template_key in ("java", "kotlin", "scala") else \
            "🦀" if template_key == "rust" else \
            "🐹" if template_key == "go" else \
            "💎" if template_key == "ruby" else \
            "🐘" if template_key == "php" else \
            "🔷" if template_key in ("dotnet", "csharp") else \
            "🐧" if template_key == "swift" else \
            "🔧" if template_key == "make" else \
            "📦" if template_key == "docker" else \
            "📂" if template_key == "git" else \
            "📝"

    print(f"\n{emoji} Created {template.display_name} project at: {project_path}")
    if created:
        print(f"   Files created: {', '.join(created)}")
    if skipped:
        print(f"   Files skipped (already exist): {', '.join(skipped)}")

    # Print next steps
    print(f"\n   Next steps:")
    print(f"   1. cd {project_path.name}")
    print(f"   2. {template.setup_cmd}")
    if template.runtime_check:
        print(f"   (Requires: {template.runtime_check})")

    return True


def list_available():
    """Print all available language templates."""
    templates = list_templates()
    print("\n📋 Available project templates:\n")
    print(f"  {'Language':<15} {'Key':<15} {'Runtime':<15} {'Setup Command'}")
    print(f"  {'-'*15} {'-'*15} {'-'*15} {'-'*30}")

    for key, tmpl in sorted(templates.items()):
        print(f"  {tmpl.display_name:<15} {key:<15} {tmpl.runtime_check:<15} {tmpl.setup_cmd}")

    print(f"\n  Total: {len(templates)} templates")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Initialize a new project for any supported language.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "language",
        nargs="?",
        help="Language key (e.g., python, go, rust, java, nodejs)",
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        help="Path for the new project (default: current directory)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List all available language templates",
    )
    parser.add_argument(
        "--overwrite", "-f",
        action="store_true",
        help="Overwrite existing scaffold files",
    )

    args = parser.parse_args()

    if args.list or args.language is None:
        list_available()
        if args.list:
            return 0

    if args.language:
        target = Path(args.project_path) if args.project_path else Path.cwd()
        success = scaffold_project(args.language, target, overwrite=args.overwrite)
        return 0 if success else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
