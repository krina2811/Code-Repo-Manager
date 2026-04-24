"""
Path Validation Helper

Validates and resolves repository paths with helpful error messages.
"""

import os
from pathlib import Path
from typing import Tuple, Optional


def validate_repo_path(repo_path: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate a repository path and provide helpful error messages.
    
    Args:
        repo_path: Path to validate
        
    Returns:
        Tuple of (is_valid, resolved_path_or_error, helpful_message)
    """
    # Handle empty/None
    if not repo_path or not repo_path.strip():
        return False, "Empty path provided", "Please provide a valid repository path"
    
    repo_path = repo_path.strip()
    
    # Check if it's a GitHub URL
    if any(x in repo_path.lower() for x in ["github.com", "gitlab.com", "bitbucket.org"]):
        return False, "Git URL not supported yet", (
            "GitHub URLs are not yet supported. Please:\n"
            "1. Clone the repository locally first:\n"
            f"   git clone {repo_path}\n"
            "2. Then use the local path"
        )
    
    # Try to resolve the path
    try:
        path = Path(repo_path).expanduser().resolve()
    except Exception as e:
        return False, f"Invalid path format: {e}", (
            f"The path '{repo_path}' has an invalid format.\n"
            "Examples of valid paths:\n"
            "  - Absolute: /home/user/my_project\n"
            "  - Relative: ./my_project\n"
            "  - Home: ~/my_project"
        )
    
    # Check if path exists
    if not path.exists():
        # Provide helpful suggestions
        parent = path.parent
        suggestions = []
        
        # Check if parent exists
        if parent.exists():
            # List what's actually in the parent
            try:
                items = list(parent.iterdir())[:5]  # First 5 items
                if items:
                    suggestions.append(f"\nContents of {parent}:")
                    for item in items:
                        suggestions.append(f"  - {item.name}")
            except PermissionError:
                suggestions.append(f"\nCannot list contents of {parent} (permission denied)")
        else:
            suggestions.append(f"\nParent directory {parent} does not exist either")
        
        # Check for common mistakes
        if "/home/" in str(path) and "/mnt/" in str(path):
            suggestions.append(
                "\n⚠️  Path mixing detected!"
                "\nYou have both /home/ and /mnt/ in your path."
                "\nIn WSL:"
                "\n  - Windows drives: /mnt/c/, /mnt/d/, /mnt/e/"
                "\n  - Linux home: /home/username/"
                f"\nDid you mean: /mnt/{str(path).split('/mnt/')[-1]}"
            )
        
        error_msg = f"Path does not exist: {path}"
        help_msg = "".join(suggestions) if suggestions else "Check the path and try again"
        
        return False, error_msg, help_msg
    
    # Check if it's a directory
    if not path.is_dir():
        return False, f"Path is not a directory: {path}", (
            f"'{path}' exists but is a file, not a directory.\n"
            "Please provide a path to a directory containing code files."
        )
    
    # Check if directory has any Python files
    python_files = list(path.rglob("*.py"))
    if not python_files:
        return False, f"No Python files found in: {path}", (
            f"The directory '{path}' exists but contains no Python files.\n"
            "Please provide a path to a Python project directory."
        )
    
    # Success!
    return True, str(path), f"Found {len(python_files)} Python file(s)"


def get_helpful_path_message() -> str:
    """Get a helpful message about path formats."""
    import platform
    
    system = platform.system()
    
    if "microsoft" in platform.uname().release.lower() or system == "Linux":
        # Likely WSL
        return """
📁 Path Format Guide (WSL):

Windows Drives:
  /mnt/c/Users/YourName/project  ← Windows C: drive
  /mnt/d/code/myproject          ← Windows D: drive
  /mnt/e/My_work/test_repo       ← Windows E: drive

Linux Paths:
  /home/krina/project            ← Your Linux home
  ~/project                      ← Same as above
  ./project                      ← Current directory

Examples:
  ✅ /mnt/e/My_work/code_repo_manager/tests/sample_repo
  ✅ /home/krina/my_projects/test_repo
  ✅ ~/code/test_repo
  ❌ /home/krina/mnt/e/...       ← Wrong! (mixing paths)
  ❌ E:/My_work/test_repo        ← Wrong! (Windows path)
"""
    elif system == "Windows":
        return """
📁 Path Format Guide (Windows):

Absolute Paths:
  C:\\Users\\YourName\\project
  D:\\code\\myproject

Forward Slashes (also work):
  C:/Users/YourName/project
  D:/code/myproject

Examples:
  ✅ C:\\Users\\krina\\projects\\test_repo
  ✅ C:/Users/krina/projects/test_repo
  ❌ /mnt/c/Users/...            ← Linux path in Windows
"""
    else:
        return """
📁 Path Format Guide:

Absolute Paths:
  /home/user/project
  /var/www/myproject

Relative Paths:
  ./project           ← Current directory
  ../project          ← Parent directory
  ~/project           ← Home directory

Examples:
  ✅ /home/krina/projects/test_repo
  ✅ ~/code/test_repo
  ✅ ./test_repo
"""


def diagnose_path_issue(repo_path: str):
    """
    Diagnose path issues and print helpful information.
    
    Args:
        repo_path: The problematic path
    """
    print("="*70)
    print("🔍 Path Diagnosis")
    print("="*70)
    
    print(f"\n📝 You provided: {repo_path}")
    
    # Validate
    is_valid, result, message = validate_repo_path(repo_path)
    
    if is_valid:
        print(f"\n✅ Path is valid!")
        print(f"   Resolved to: {result}")
        print(f"   {message}")
    else:
        print(f"\n❌ Path is invalid!")
        print(f"   Error: {result}")
        print(f"\n💡 Help:")
        print(f"   {message}")
        
        # Show path format guide
        print(get_helpful_path_message())
        
        # Show current working directory
        print(f"\n📍 Current directory: {os.getcwd()}")
        
        # Show sample_repo path if it exists
        try:
            from config.settings import PROJECT_ROOT
            sample_repo = PROJECT_ROOT / "tests" / "sample_repo"
            if sample_repo.exists():
                print(f"\n✨ Try the built-in sample repository:")
                print(f"   {sample_repo}")
        except:
            pass
    
    print("="*70)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        diagnose_path_issue(sys.argv[1])
    else:
        print("Usage: python path_validator.py <path_to_check>")
        print("\nExample:")
        print("  python path_validator.py /mnt/e/My_work/test_repo")