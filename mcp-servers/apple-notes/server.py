#!/usr/bin/env python3.13
"""
MCP Server — Apple Notes
Gives Claude access to Apple Notes via AppleScript.
"""

import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("apple-notes")

SEP = "|||"  # record separator unlikely to appear in note content


def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


@mcp.tool()
def get_recent_notes(days: int = 7) -> str:
    """Get notes modified in the last N days (default 7). Returns title, folder, and modification date."""
    script = f"""
tell application "Notes"
    set sep to "{SEP}"
    set cutoff to (current date) - ({days} * 86400)
    set allNotes to every note
    set output to ""
    repeat with n in allNotes
        set theNote to contents of n
        set modDate to modification date of theNote
        if modDate > cutoff then
            set c to container of theNote
            tell c to set folderName to name
            set output to output & (name of theNote) & " | " & folderName & " | " & (modDate as string) & sep
        end if
    end repeat
    return output
end tell
"""
    output = run_applescript(script)
    if not output:
        return f"No notes modified in the last {days} days."
    lines = [l.strip() for l in output.split(SEP) if l.strip()]
    formatted = "\n".join(f"- {l}" for l in lines)
    return f"Notes modified in last {days} days ({len(lines)} total):\n{formatted}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Search Apple Notes by keyword. Returns matching note titles, folders, and snippets."""
    script = f"""
tell application "Notes"
    set sep to "{SEP}"
    set q to "{query.replace('"', '\\"')}"
    set allNotes to every note
    set output to ""
    repeat with n in allNotes
        set theNote to contents of n
        set noteBody to plaintext of theNote
        set noteName to name of theNote
        if noteBody contains q or noteName contains q then
            if length of noteBody > 200 then
                set snippet to text 1 thru 200 of noteBody
            else
                set snippet to noteBody
            end if
            set c to container of theNote
            tell c to set folderName to name
            set output to output & noteName & " [" & folderName & "]: " & snippet & sep
        end if
    end repeat
    return output
end tell
"""
    output = run_applescript(script)
    if not output:
        return f"No notes found matching '{query}'."
    lines = [l.strip() for l in output.split(SEP) if l.strip()]
    formatted = "\n\n---\n".join(lines[:20])
    suffix = f"\n\n...and {len(lines) - 20} more." if len(lines) > 20 else ""
    return f"Found {len(lines)} note(s) matching '{query}':\n\n{formatted}{suffix}"


@mcp.tool()
def get_note(title: str, folder: str = "") -> str:
    """Get the full content of a note by title. Optionally specify folder to disambiguate."""
    if folder:
        script = f"""
tell application "Notes"
    set f to folder "{folder.replace('"', '\\"')}"
    set n to first note of f whose name is "{title.replace('"', '\\"')}"
    return plaintext of n
end tell
"""
    else:
        script = f"""
tell application "Notes"
    set n to first note whose name is "{title.replace('"', '\\"')}"
    return plaintext of n
end tell
"""
    output = run_applescript(script)
    return output if output else f"Note '{title}' not found."


@mcp.tool()
def list_folders() -> str:
    """List all Apple Notes folders/accounts."""
    script = """
tell application "Notes"
    set results to {}
    repeat with f in every folder
        set end of results to (name of f) & " (" & (count every note of f) & " notes)"
    end repeat
    return results
end tell
"""
    output = run_applescript(script)
    lines = [l.strip() for l in output.split(",") if l.strip()]
    return "Apple Notes folders:\n" + "\n".join(f"- {l}" for l in lines)


@mcp.tool()
def get_all_note_titles() -> str:
    """Get titles of all notes with their folders — useful for browsing what exists."""
    script = f"""
tell application "Notes"
    set sep to "{SEP}"
    set allNotes to every note
    set output to ""
    repeat with n in allNotes
        set theNote to contents of n
        set c to container of theNote
        tell c to set folderName to name
        set output to output & folderName & "/" & (name of theNote) & sep
    end repeat
    return output
end tell
"""
    output = run_applescript(script)
    lines = sorted(l.strip() for l in output.split(SEP) if l.strip())
    return f"All notes ({len(lines)} total):\n" + "\n".join(f"- {l}" for l in lines)


if __name__ == "__main__":
    mcp.run()
