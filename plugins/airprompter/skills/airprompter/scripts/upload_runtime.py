#!/usr/bin/env python3
"""Bundled skill-upload helper: pack a local skill folder (or check a .zip) and stage it for import.

Only the bundled `upload` launcher is a host entrypoint. Data-only: no workflow execution, no
shell evaluation, no credential custody. The single-use upload token arrives by token file or
pipe (install_runtime's rules) and is sent once, to the pinned API origin only.

The packing rules are a port of the server's upload reader, not an approximation:
packages/domain/src/workflowFiles/importUploadPackage.ts (readImportUploadArchive) on
packages/domain/src/skills/zipRead.ts (readZipEntries, strict) with the path rules of
workflowFileTree.ts (rejectWorkflowFilePath) and contracts imports/skillImport.ts
(rejectSkillImportPath, SKILL_IMPORT_TEXT_EXTENSIONS and limits). A zip the server would refuse
is refused here, before any network call, and an upload is sent only when it passes.
"""
import argparse
import base64
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import ssl
import stat
import struct
import sys
import unicodedata
import urllib.error
import urllib.request
import zipfile
import zlib


def _load_install_runtime():
    # The sibling install_runtime.py, by path: the working directory can never select another module.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install_runtime.py")
    spec = importlib.util.spec_from_file_location("airprompter_install_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = _load_install_runtime()
Refusal = base.Refusal
API_ORIGINS = base.API_ORIGINS
TOKEN = base.TOKEN

# --- The server's limits (contracts + domain), spelled here. -------------------------------------
MAX_ZIP_BYTES = 4_000_000            # POST /v1/skill-uploads `bytes` ceiling (Lambda payload)
MAX_TEXT_FILES = 40                  # SKILL_IMPORT_MAX_FILES
MAX_OTHER_FILES = 200                # SKILL_IMPORT_MAX_OTHER_PATHS
MAX_TEXT_CHARS = 200_000             # SKILL_IMPORT_MAX_FILE_CHARS (UTF-16 code units, after decoding)
MAX_FILE_BYTES = 1_000_000           # WORKFLOW_FILE_LIMITS.maxFileBytes (IMPORT_UPLOAD_ZIP_LIMITS.maxEntryBytes)
MAX_AGENT_FILE_BYTES = 262_144       # WORKFLOW_FILE_CHANGE_INLINE_MAX_FILE_BYTES: what an MCP import stores per file
MAX_PATH_CHARS = 400                 # WORKFLOW_FILE_LIMITS.maxPathChars (UTF-16 code units)
ZIP_MAX_ENTRIES = 400                # IMPORT_UPLOAD_ZIP_LIMITS.maxEntries
ZIP_MAX_TOTAL = 8_000_000            # IMPORT_UPLOAD_ZIP_LIMITS.maxTotalBytes
TEXT_EXTENSIONS = (".md", ".markdown", ".txt")  # SKILL_IMPORT_TEXT_EXTENSIONS
# contracts/imports/invisiblePathCharacters.ts INVISIBLE_PATH_CHARACTERS.
INVISIBLE = frozenset("\u200b\u2060\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")
# What JavaScript's String.prototype.trim removes (Python's strip() differs: \x1c-\x1f, \x85).
JS_WHITESPACE = "\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# importUploadPackage.ts SKILL_FILE = /(^|\/)skill\.md$/i (JS /i without u: ASCII case folding only).
SKILL_FILE = re.compile(r"(^|/)skill\.md$", re.IGNORECASE | re.ASCII)
# Helper policy (not the server's): names that usually hold a secret are never sent; the person decides.
SECRET_NAMES = ("id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*service-account*.json", "credentials", ".env*", ".npmrc", ".netrc")
MAX_SCAN_ENTRIES = 50_000
MAX_RESPONSE = 65_536
UPLOAD_TIMEOUT = 60
# contracts v1/skillUpload.ts SKILL_UPLOAD_ID_PATTERN (a lowercase UUID).
UPLOAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

ERROR_CODES = {404: "upload_token_invalid", 400: "upload_rejected", 413: "upload_too_large", 403: "upload_refused", 429: "upload_rate_limited", 503: "upload_unavailable"}
# Fixed sentences for the person: never server, file-content or token data (paths are the person's own and are named).
MESSAGES = {
    "upload_token_invalid": "This upload expired or was already used.",
    "upload_rejected": "AirPrompter could not accept this upload.",
    "upload_too_large": "This upload is larger than AirPrompter accepts (4 MB zipped).",
    "upload_refused": "You can't upload skills to that library right now.",
    "upload_rate_limited": "Too many uploads right now; try again later.",
    "upload_unavailable": "The upload could not be completed.",
    "upload_response_invalid": "AirPrompter's answer could not be read.",
    "redirect_refused": "The upload was redirected, which is never followed.",
    "tls_certificates_unavailable": "This computer's Python cannot check secure web connections yet.",
    "platform_unvalidated": "Local uploads are not validated on this operating system yet.",
    "token_file_refused": "The upload could not start.",
    "interactive_stdin": "The upload could not start.",
    "stdin_empty": "The upload could not start.",
    "stdin_timeout": "The upload could not start.",
    "invalid_token_request": "The upload could not start.",
    "invalid_arguments": "The upload could not start.",
    "state_dir_not_writable": "~/.airprompter is not writable here.",
    "private_directory_required": "~/.airprompter is open to other users or could not be made private.",
}
# For the agent only (`hint`), never shown as the person's message.
HINTS = {
    "token_file_refused": "The token file was refused: it must be your own new file directly in tokenFolder, at most 512 bytes.",
    "interactive_stdin": "The token must arrive in a token file or a pipe, never an interactive terminal.",
    "stdin_empty": "No token arrived on the pipe.",
    "stdin_timeout": "The token pipe never closed.",
    "invalid_token_request": 'The token file must hold exactly {"token":"<token>"}.',
    "invalid_arguments": "Usage: upload [--plan] [--force] [--token-file <file>] --target prod|dev <folder-or-.zip>",
    "upload_token_invalid": "The token expired or was used. Call start_skill_upload once more for a new one; if that fails too, import with files[].",
}
TOKEN_USED_HINT = "The token is used up: never send it again. For another try, call start_skill_upload for a new token."
TOKEN_UNUSED_HINT = "The token was not used. Delete the file you wrote if it is still there, then write the token once more to a new file in tokenFolder with your file-write tool (or pipe it, only as references/local-install.md describes)."
TOKEN_UNUSED = {"token_file_refused", "interactive_stdin", "stdin_empty", "stdin_timeout", "invalid_token_request", "state_dir_not_writable", "private_directory_required", "unsafe_filesystem"}
FILE_OVER_AGENT_LIMIT = "{path} is over 256 KB; AirPrompter can't take files that large from here yet."


class UploadError(Exception):
    """A refusal with a fixed code and a sentence for the person (never token or server data)."""

    def __init__(self, code, message=None, **extra):
        super().__init__(code)
        self.code = code
        self.message = message or MESSAGES.get(code, "The upload could not be prepared.")
        self.extra = extra


def utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def js_trim(text):
    return text.strip(JS_WHITESPACE)


def decode_text(data):
    # TextDecoder('utf-8', { fatal: false }): replacement characters, a leading BOM removed.
    return data.decode("utf-8-sig", "replace")


def reject_workflow_file_path(path):
    """workflowFileTree.ts rejectWorkflowFilePath."""
    if utf16_length(path) == 0 or utf16_length(path) > MAX_PATH_CHARS:
        return "path length"
    if path.startswith("/") or re.match(r"^[a-zA-Z]:", path):
        return "absolute path"
    if "\\" in path:
        return "backslash"
    if re.search(r"[\x00-\x1f\x7f]", path):
        return "control character"
    if any(character in INVISIBLE for character in path):
        return "invisible or direction-changing character"
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            return 'empty, "." or ".." segment'
        if segment != js_trim(segment):
            return "leading or trailing whitespace"
    return None


def reject_skill_import_path(path):
    """contracts rejectSkillImportPath(path, { anyExtension: true })."""
    if utf16_length(path) == 0 or utf16_length(path) > 400:
        return "path length"
    if path.startswith("/") or re.match(r"^[a-zA-Z]:", path):
        return "absolute path"
    if any(segment == ".." for segment in re.split(r"[\\/]", path)):
        return "path traversal"
    if re.search(r"[\x00-\x1f\x7f]", path):
        return "control character"
    if any(character in INVISIBLE for character in path):
        return "invisible or direction-changing character"
    return None


def reject_upload_path(path):
    """importUploadPackage.ts rejectUploadPath."""
    if path.endswith("/"):
        return "directory entry"
    return reject_workflow_file_path(path) or reject_skill_import_path(path)


def is_litter(path):
    """importUploadPackage.ts isUploadLitter: dropped silently."""
    segments = path.split("/")
    name = segments[-1]
    return "__MACOSX" in segments or name == ".DS_Store" or name.startswith("._") or name in ("Thumbs.db", "desktop.ini")


def is_ignored_relative(relative):
    """skillFolderMapping.ts isIgnoredSkillUploadPath, on the path inside its skill folder."""
    segments = relative.split("/")
    return "__MACOSX" in segments or any(s.startswith(".") for s in segments) or segments[-1] in ("Thumbs.db", "desktop.ini")


def is_text(path):
    """importUploadPackage.ts isUploadText: the server's text/other split."""
    return path.lower().endswith(TEXT_EXTENSIONS)


def is_secret_name(path):
    name = path.split("/")[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAMES)


def has_install_segment(path):
    return ".airprompter" in path.split("/")


def skill_folder_of(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def owning_folder(path, folders):
    """The deepest skill folder holding `path`, or None (a loose file)."""
    best = None
    for folder in folders:
        if folder == "" or path.startswith(folder + "/"):
            if best is None or len(folder) > len(best):
                best = folder
    return best


def collision_key(path):
    return unicodedata.normalize("NFC", path).lower()


def shown(paths, limit=3):
    paths = list(paths)
    return ", ".join(paths[:limit]) + (", ..." if len(paths) > limit else "")


# --- The strict reader (zipRead.ts readZipEntries, strict: true) ---------------------------------
EOCD = 0x06054B50
CENTRAL = 0x02014B50
LOCAL = 0x04034B50
ZIP64_LOCATOR = 0x07064B50


def damaged(message="That archive is damaged and cannot be read."):
    return UploadError("zip_damaged", message)


def inflate(raw, cap):
    # DecompressionStream('deflate-raw') read up to `cap`: None past the cap, damaged on a bad stream.
    decompressor = zlib.decompressobj(-15)
    try:
        out = decompressor.decompress(raw, cap + 1)
    except zlib.error:
        raise damaged()
    if len(out) > cap:
        return None
    if not decompressor.eof or decompressor.unused_data:
        raise damaged()
    return out


def read_zip_entries(archive):
    """Every entry of an archive as bytes, refusing exactly what the server's strict reader refuses."""
    size = len(archive)
    if size < 22:
        raise UploadError("not_zip", "That file is not a ZIP archive.")
    u16 = lambda at: struct.unpack_from("<H", archive, at)[0]
    u32 = lambda at: struct.unpack_from("<I", archive, at)[0]

    def consistent_at(index):
        comment, central_size, central_offset, count = u16(index + 20), u32(index + 12), u32(index + 16), u16(index + 10)
        return index + 22 + comment == size and central_offset + central_size <= index and (
            count == 0 or (central_offset + 4 <= size and u32(central_offset) == CENTRAL))

    eocd, other = -1, 0
    floor = max(0, size - 0xFFFF - 22)
    end = size - 22 + 4
    while True:
        index = archive.rfind(b"PK\x05\x06", floor, end)
        if index < 0:
            break
        if eocd < 0:
            eocd = index
        elif consistent_at(index):
            other += 1
        end = index + 3
    if eocd >= 0 and (not consistent_at(eocd) or other > 0):
        raise damaged("That archive is damaged: its end-of-archive record is not where a ZIP puts it.")
    if eocd < 0:
        raise UploadError("not_zip", "That file is not a ZIP archive.")
    zip64 = UploadError("zip64", "That archive uses ZIP64, which a skill never needs. Re-create it as a standard ZIP.")
    if eocd >= 20 and u32(eocd - 20) == ZIP64_LOCATOR:
        raise zip64
    count = u16(eocd + 10)
    if count == 0xFFFF or u32(eocd + 16) == 0xFFFFFFFF:
        raise zip64
    if count > ZIP_MAX_ENTRIES:
        raise UploadError("too_many_entries", f"That archive has {count} entries; {ZIP_MAX_ENTRIES} is the most a skill may carry.")
    offset = u32(eocd + 16)
    skipped, pending, seen = [], [], {}
    declared = 0
    for _ in range(count):
        if offset + 46 > size or u32(offset) != CENTRAL:
            raise damaged()
        flags, method = u16(offset + 8), u16(offset + 10)
        crc, compressed, uncompressed = u32(offset + 16), u32(offset + 20), u32(offset + 24)
        name_length, extra_length, comment_length = u16(offset + 28), u16(offset + 30), u16(offset + 32)
        made_by, external, local = u16(offset + 4), u32(offset + 38), u32(offset + 42)
        name_start = offset + 46
        path = decode_text(archive[name_start:name_start + name_length])
        offset += 46 + name_length + extra_length + comment_length
        if 0xFFFFFFFF in (compressed, uncompressed, local):
            raise zip64
        if made_by >> 8 == 3 and ((external >> 16) & 0xF000) == 0xA000:
            skipped.append((path, "shortcut"))
            continue
        rejection = reject_upload_path(path)
        if rejection:
            skipped.append((path, rejection))
            continue
        key = collision_key(path)
        if key in seen:
            if seen[key] == path:
                raise UploadError("duplicate_path", f"That archive is damaged: {path} appears twice.")
            raise UploadError("path_collision", f"That archive has {seen[key]} and {path}, which one folder cannot hold apart (case or accents).")
        seen[key] = path
        if flags & 0x0001:
            skipped.append((path, "encrypted"))
            continue
        if uncompressed > MAX_FILE_BYTES:
            skipped.append((path, "file too large"))
            continue
        declared += uncompressed
        if declared > ZIP_MAX_TOTAL:
            raise UploadError("too_large", "That archive expands to more than a skill should contain.")
        if method not in (0, 8):
            skipped.append((path, "unsupported compression"))
            continue
        pending.append((path, method, crc, compressed, uncompressed, local, name_start, name_length))
    entries, actual = [], 0
    for path, method, crc, compressed, uncompressed, local, name_start, name_length in pending:
        if local + 30 > size or u32(local) != LOCAL:
            raise damaged()
        local_name_length, local_extra_length = u16(local + 26), u16(local + 28)
        data_start = local + 30 + local_name_length + local_extra_length
        if data_start + compressed > size:
            raise damaged()
        if archive[local + 30:local + 30 + local_name_length] != archive[name_start:name_start + name_length]:
            raise damaged()
        if method == 0 and compressed != uncompressed:
            raise damaged()
        raw = archive[data_start:data_start + compressed]
        data = raw if method == 0 else inflate(raw, min(uncompressed, MAX_FILE_BYTES))
        if data is None or len(data) != uncompressed:
            raise UploadError("size_mismatch", f"That archive is damaged: {path} is not the size its header states.")
        actual += len(data)
        if actual > ZIP_MAX_TOTAL:
            raise UploadError("too_large", "That archive expands to more than a skill should contain.")
        if zlib.crc32(data) & 0xFFFFFFFF != crc:
            raise UploadError("crc_mismatch", f"That archive is damaged: {path} does not match its checksum.")
        entries.append((path, bytes(data)))
    return entries, skipped


# --- What the server keeps (importUploadPackage.ts readImportUploadArchive) ----------------------
def check_counts_and_text(kept):
    texts = sorted(p for p in kept if is_text(p))
    others = sorted(p for p in kept if not is_text(p))
    if len(texts) > MAX_TEXT_FILES:
        raise UploadError("too_many_text_files", f"This upload has {len(texts)} text files in its skill folders; one import reads at most {MAX_TEXT_FILES}. Import fewer skills at a time.")
    if len(others) > MAX_OTHER_FILES:
        raise UploadError("too_many_other_files", f"This upload has {len(others)} other files in its skill folders; one import stores at most {MAX_OTHER_FILES}. Import fewer skills at a time.")
    for path in texts:
        if utf16_length(decode_text(kept[path])) > MAX_TEXT_CHARS:
            raise UploadError("text_file_too_large", f"{path} is over {MAX_TEXT_CHARS:,} characters, more than one imported text file may hold.")


def analyze_archive(archive):
    """The server's reading of an archive: kept bytes by path, plus what it leaves out and why."""
    entries, skipped = read_zip_entries(archive)
    refused = [(p, r) for p, r in skipped if r != "directory entry" and not (is_litter(p) and reject_workflow_file_path(p) is None)]
    if refused:
        raise UploadError("zip_entry_refused", f"Some files in that archive cannot be imported: {shown(f'{p} ({r})' for p, r in refused)}. Remove them and try again.")
    usable = [(p, b) for p, b in entries if not is_litter(p)]
    folders = sorted({skill_folder_of(p) for p, _ in usable if SKILL_FILE.search(p)})
    if not folders:
        raise UploadError("no_skill_md", "That archive holds no SKILL.md to import.")
    kept, left_out = {}, []
    for path, data in usable:
        folder = owning_folder(path, folders)
        if folder is None:
            left_out.append({"path": path, "reason": "outside every skill folder"})
        elif is_ignored_relative(path[len(folder) + 1:] if folder else path):
            left_out.append({"path": path, "reason": "hidden"})
        else:
            kept[path] = data
    check_counts_and_text(kept)
    return kept, sorted(left_out, key=lambda e: e["path"]), folders


# --- Folder input --------------------------------------------------------------------------------
def refuse_broad_root(root):
    home = Path(os.path.realpath(Path.home()))
    if root == home or root in home.parents or root == Path(root.anchor) or root.parent == root:
        raise UploadError("folder_too_broad", "That folder is your home folder or above it. Choose the skill's own folder.")


def scan(root):
    """(relative posix path, absolute path, kind) for every entry under root, never following a link."""
    found = []

    def failed(error):
        raise UploadError("folder_unreadable", f"Part of that folder could not be read ({type(error).__name__}).")

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in sorted(dirs + files):
            target = Path(directory) / name
            info = os.lstat(target)
            relative = target.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                continue
            kind = "file" if stat.S_ISREG(info.st_mode) else "link" if stat.S_ISLNK(info.st_mode) else "special"
            found.append((relative, target, kind, info))
            if len(found) > MAX_SCAN_ENTRIES:
                raise UploadError("folder_too_large", f"That folder holds more than {MAX_SCAN_ENTRIES:,} files. Choose the skill's own folder.")
    return found


def read_regular(target, info, limit=MAX_FILE_BYTES):
    # Open without following a link and check it is the file that was listed.
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise UploadError("file_unreadable", f"{target.name} could not be read.")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise UploadError("file_changed", f"{target.name} changed while it was being read.")
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def collapse(path, stop_at):
    """The shortest prefix of `path` (as a folder, with a trailing /) that is not in `stop_at`, else the path."""
    segments = path.split("/")
    for index in range(1, len(segments)):
        prefix = "/".join(segments[:index])
        if prefix not in stop_at:
            return prefix + "/"
    return path


def install_record(folder_path):
    """The `.airprompter/install.json` a local install wrote into this skill folder, or None."""
    record = folder_path / ".airprompter" / "install.json"
    try:
        if not stat.S_ISREG(os.lstat(record).st_mode):
            return None
        return base.metadata(base.read_json(base.read_file(record, base.MAX_RESPONSE)))
    except (Refusal, OSError, ValueError, UnicodeDecodeError):
        return None


def pack_folder(root):
    refuse_broad_root(root)
    prefix = root.name
    found = scan(root)
    regular = {f"{prefix}/{rel}" for rel, _, kind, _ in found if kind == "file"}
    candidates = [p for p in regular if not is_litter(p) and not has_install_segment(p)]
    folders = sorted({skill_folder_of(p) for p in candidates if SKILL_FILE.search(p)})
    if not folders:
        raise UploadError("no_skill_md", "That folder holds no SKILL.md, so there is no skill to upload.")
    ancestors = {"/".join(f.split("/")[:i]) for f in folders for i in range(1, len(f.split("/")) + 1)}
    records = {folder: install_record(root.parent / folder) for folder in folders}
    kept, left_out = {}, set()
    for relative, target, kind, info in found:
        path = f"{prefix}/{relative}"
        if is_litter(path) or has_install_segment(path):
            continue
        folder = owning_folder(path, folders)
        if folder is None:
            left_out.add((collapse(path, ancestors), "outside every skill folder"))
            continue
        inside = path[len(folder) + 1:]
        if is_ignored_relative(inside):
            hidden = inside.split("/")
            cut = next(i for i, s in enumerate(hidden) if s.startswith(".") or s in ("__MACOSX", "Thumbs.db", "desktop.ini"))
            left_out.add((folder + "/" + "/".join(hidden[:cut + 1]) + ("/" if cut + 1 < len(hidden) else ""), "hidden"))
            continue
        if records[folder] is not None and inside == "SHA256SUMS":
            left_out.add((path, "install record"))
            continue
        if kind == "link":
            raise UploadError("symlink_refused", f"{path} is a shortcut (symbolic link). An upload carries files, not shortcuts: replace it with the file, or remove it.")
        if kind != "file":
            raise UploadError("special_file_refused", f"{path} is not a regular file. Remove it from the skill folder.")
        if is_secret_name(path):
            raise UploadError("secret_file_refused", f"{path} looks like a key or credentials file, so nothing was uploaded. Move it out of the skill folder (or rename it if it holds no secret), then try again.")
        rejection = reject_upload_path(path)
        if rejection:
            raise UploadError("path_refused", f"{path} cannot be imported ({rejection}). Rename it, then try again.")
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise UploadError("path_refused", f"A file name under {folder} is not valid text. Rename it, then try again.")
        if info.st_size > MAX_AGENT_FILE_BYTES:
            raise UploadError("file_too_large", FILE_OVER_AGENT_LIMIT.format(path=path))
        data = read_regular(target, info, MAX_AGENT_FILE_BYTES)
        if data is None:
            raise UploadError("file_too_large", FILE_OVER_AGENT_LIMIT.format(path=path))
        kept[path] = data
    seen = {}
    for path in sorted(kept):
        key = collision_key(path)
        if key in seen:
            raise UploadError("path_collision", f"{seen[key]} and {path} cannot be told apart (case or accents). Rename one, then try again.")
        seen[key] = path
    check_counts_and_text(kept)
    return kept, sorted(({"path": p, "reason": r} for p, r in left_out), key=lambda e: e["path"]), folders, records


def build_zip(kept):
    """Deterministic: sorted POSIX paths, fixed time and mode, deflate, no directory entries, no ZIP64."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
        for path in sorted(kept):
            info = zipfile.ZipInfo(path, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, kept[path], compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return buffer.getvalue()


def compare_with_installs(kept, folders, records):
    """(identity, changedFromInstall, upToDate) from each skill folder's install record."""
    changed, complete = [], bool(folders)
    for folder in folders:
        record = records.get(folder)
        if record is None:
            complete = False
            continue
        installed = {f["path"]: f["sha256"] for f in record["files"]}
        current = {p[len(folder) + 1:]: base.digest(b) for p, b in kept.items() if owning_folder(p, folders) == folder}
        for inside in sorted(set(installed) | set(current)):
            if installed.get(inside) != current.get(inside):
                changed.append(f"{folder}/{inside}")
    present = [records[f] for f in folders if records.get(f) is not None]
    identity = {"workflowId": present[0]["identity"]["workflowId"]} if len(folders) == 1 and present else None
    return identity, (sorted(changed) if present else None), complete and not changed


def pack(given):
    source = Path(os.path.realpath(os.path.abspath(given)))
    try:
        info = os.lstat(source)
    except OSError:
        raise UploadError("input_missing", "That folder or file does not exist.")
    if stat.S_ISDIR(info.st_mode):
        kept, left_out, folders, records = pack_folder(source)
        archive = build_zip(kept)
        if len(archive) > MAX_ZIP_BYTES:
            raise UploadError("upload_too_large", f"The zipped skill is {len(archive):,} bytes; an upload carries at most {MAX_ZIP_BYTES:,}.")
        # The bytes we send must read back, under the server's rules, as exactly the files we packed.
        checked, _, _ = analyze_archive(archive)
        if checked != kept:
            raise UploadError("pack_mismatch", "The packed zip did not read back as the files it was made from.")
        identity, changed, up_to_date = compare_with_installs(kept, folders, records)
    elif stat.S_ISREG(info.st_mode) and source.name.lower().endswith(".zip"):
        if info.st_size > MAX_ZIP_BYTES:
            raise UploadError("upload_too_large", f"That zip is {info.st_size:,} bytes; an upload carries at most {MAX_ZIP_BYTES:,}.")
        archive = read_regular(source, info, MAX_ZIP_BYTES)
        if archive is None:
            raise UploadError("upload_too_large", f"That zip is larger than {MAX_ZIP_BYTES:,} bytes.")
        # Sent as-is only when it passes: never re-packed.
        kept, left_out, folders = analyze_archive(archive)
        for path in sorted(kept):
            if len(kept[path]) > MAX_AGENT_FILE_BYTES:
                raise UploadError("file_too_large", FILE_OVER_AGENT_LIMIT.format(path=path))
        for path, _ in read_zip_entries(archive)[0]:
            if has_install_segment(path):
                raise UploadError("install_record_in_zip", f"That zip holds {path}, AirPrompter's own install record, which is never uploaded. Zip the skill folder without its .airprompter folder.")
            if is_secret_name(path) and not is_litter(path):
                raise UploadError("secret_file_refused", f"That zip holds {path}, which looks like a key or credentials file, so nothing was uploaded. Remove it from the zip, then try again.")
        identity, changed, up_to_date = None, None, False
    else:
        raise UploadError("unsupported_input", "Give a skill folder or a .zip file.")
    result = {
        "sha256": base.digest(archive),
        "bytes": len(archive),
        "fileCount": len(kept),
        "files": [{"path": p, "sha256": base.digest(kept[p]), "bytes": len(kept[p])} for p in sorted(kept)],
        "leftOut": left_out,
        **({"identity": identity} if identity else {}),
        **({"changedFromInstall": changed} if changed is not None else {}),
    }
    return archive, result, up_to_date


# --- The one network call ------------------------------------------------------------------------
def open_url(request, context=None):
    handlers = [base.NoRedirect(), *([urllib.request.HTTPSHandler(context=context)] if context is not None else [])]
    return urllib.request.build_opener(*handlers).open(request, timeout=UPLOAD_TIMEOUT)


def open_verified(request):
    # A certificate failure happens in the handshake, before any request byte: the token was not sent.
    try:
        return open_url(request, base.TLS["context"])
    except (urllib.error.URLError, ssl.SSLError) as error:
        if not base.certificate_failure(error):
            raise
        if base.TLS["context"] is not None:
            raise UploadError("tls_certificates_unavailable", tokenUsed=False)
    context = base.fallback_context()
    if context is None:
        raise UploadError("tls_certificates_unavailable", tokenUsed=False)
    try:
        response = open_url(request, context)
    except (urllib.error.URLError, ssl.SSLError) as error:
        if base.certificate_failure(error):
            raise UploadError("tls_certificates_unavailable", tokenUsed=False)
        raise
    base.TLS["context"] = context
    return response


def bounded(stream):
    data = stream.read(MAX_RESPONSE + 1) if stream is not None else b""
    return data if len(data) <= MAX_RESPONSE else b""


def http_error(error):
    status = error.code
    code = ERROR_CODES.get(status, "upload_unavailable")
    try:
        body = base.read_json(bounded(error))
    except Exception:
        body = {}
    finally:
        with contextlib.suppress(Exception):
            error.close()
    body = body if isinstance(body, dict) else {}
    extra = {"tokenUsed": True}
    if code == "upload_rate_limited":
        retry = body.get("retryAfterSeconds")
        header = error.headers.get("Retry-After") if error.headers else None
        if type(retry) is int and 0 <= retry <= 86_400:
            extra["retryAfterSeconds"] = retry
        elif header and header.isdigit():
            extra["retryAfterSeconds"] = min(int(header), 86_400)
    # The person's message is ours, never the server's.
    return UploadError(code, **extra)


def post_upload(url, token, archive, sha256):
    """POST {token, sha256, bytes, zipBase64}; any failure after the token left is `tokenUsed`."""
    body = base.json_bytes({"token": token, "sha256": sha256, "bytes": len(archive), "zipBase64": base64.b64encode(archive).decode("ascii")})
    request = urllib.request.Request(url, data=body, method="POST", headers={"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"})
    try:
        response = open_verified(request)
    except UploadError:
        raise
    except urllib.error.HTTPError as error:
        raise http_error(error)
    except Refusal as error:
        raise UploadError(str(error), tokenUsed=True)
    except Exception:
        raise UploadError("upload_unavailable", tokenUsed=True)
    try:
        with response:
            if response.status != 201 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise UploadError("upload_response_invalid", tokenUsed=True)
            answer = base.read_json(bounded(response))
    except UploadError:
        raise
    except Exception:
        raise UploadError("upload_response_invalid", tokenUsed=True)
    if not (isinstance(answer, dict) and set(answer) == {"uploadId", "sha256", "bytes", "expiresAt"}
            and isinstance(answer["uploadId"], str) and UPLOAD_ID.fullmatch(answer["uploadId"])
            and answer["sha256"] == sha256 and answer["bytes"] == len(archive) and isinstance(answer["expiresAt"], str)):
        raise UploadError("upload_response_invalid", tokenUsed=True)
    return answer


def read_token(token_file):
    try:
        data = base.token_file(token_file) if token_file else base.private_stdin(base.MAX_TOKEN_REQUEST)
    except Refusal as error:
        raise UploadError(str(error), tokenUsed=False)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        value = base.read_json(data)
    except Exception:
        raise UploadError("invalid_token_request", tokenUsed=False)
    if not (isinstance(value, dict) and set(value) == {"token"} and isinstance(value["token"], str) and TOKEN.fullmatch(value["token"])):
        raise UploadError("invalid_token_request", tokenUsed=False)
    return value["token"]


def token_folder():
    with contextlib.suppress(Exception):
        return str(base.state_dir("tokens"))
    return None


def summary(result):
    left = result["leftOut"]
    reasons = {}
    for entry in left:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    outcome = {
        "uploaded": "Uploaded; ready to preview and import.",
        "planned": "Plan only: nothing was sent.",
        "up_to_date": "Already up to date with the installed copy: nothing was sent.",
    }[result["status"]]
    return "\n".join([
        f"Packed {result['fileCount']} file(s), {result['bytes']:,} bytes zipped.",
        "Left out: " + (", ".join(f"{n} {r}" for r, n in sorted(reasons.items())) if left else "nothing") + ".",
        outcome,
    ])


def run(args):
    if os.name == "nt":
        raise UploadError("platform_unvalidated")
    pin = API_ORIGINS[args.target]
    if args.plan and args.token_file:
        raise UploadError("invalid_arguments", tokenUsed=False, hint="--plan takes no token; the token file was deleted unused.")
    archive, result, up_to_date = pack(args.path)
    # --force: a deliberate second copy of an unchanged installed skill.
    if up_to_date and not args.force:
        return {"status": "up_to_date", **result}
    if args.plan:
        folder = token_folder()
        return {"status": "planned", **result, **({"tokenFolder": folder} if folder else {})}
    token = read_token(args.token_file)
    url = pin + "/v1/skill-uploads"
    base.origin(url)
    answer = post_upload(url, token, archive, result["sha256"])
    # The per-file list is the plan's; the upload answers its count.
    counted = {key: value for key, value in result.items() if key != "files"}
    return {"status": "uploaded", "uploadId": answer["uploadId"], "sourceUrl": f"{pin}/v1/skill-uploads/{answer['uploadId']}", "expiresAt": answer["expiresAt"], **counted}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes values, potentially a misplaced bearer.
        # The one JSON object goes to stdout, as on every other exit.
        print(json.dumps({"status": "error", "code": "invalid_arguments", "message": MESSAGES["invalid_arguments"], "hint": HINTS["invalid_arguments"]}))
        sys.exit(2)


def main(argv=None):
    # No abbreviations: a misplaced `--token <bearer>` must fail parsing, never become `--token-file`.
    parser = SafeArgumentParser(description="Bundled AirPrompter skill upload helper; the token arrives by token file or stdin only.", allow_abbrev=False)
    parser.add_argument("path")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--token-file")
    parser.add_argument("--target", choices=sorted(API_ORIGINS), default="prod")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        value = run(args)
        print(base.json_bytes(value).decode("utf-8"))
        print(summary(value), file=sys.stderr)
        return 0
    except Exception as error:
        if isinstance(error, UploadError):
            code, message, extra = error.code, error.message, dict(error.extra)
        elif isinstance(error, Refusal):
            code, message, extra = str(error), MESSAGES.get(str(error), "The upload could not be prepared."), {}
        else:
            # OS/HTTP exceptions can carry paths or URLs with secrets: fixed outcome only.
            code, message, extra = "upload_unavailable", MESSAGES["upload_unavailable"], {}
        if code in TOKEN_UNUSED:
            extra.setdefault("tokenUsed", False)
        hints = [extra.pop("hint", None) or HINTS.get(code)]
        if extra.get("tokenUsed") is True:
            hints.append(TOKEN_USED_HINT)
        elif extra.get("tokenUsed") is False and code in TOKEN_UNUSED:
            hints.append(TOKEN_UNUSED_HINT)
        if any(hints):
            extra["hint"] = " ".join(hint for hint in hints if hint)
        print(json.dumps({"status": "error", "code": code, "message": message, **extra}, ensure_ascii=False))
        return 1
    finally:
        # A token file inside the tokens folder never outlives this process, on any exit path.
        leftover = base.token_folder_entry(args.token_file)
        if leftover is not None:
            with contextlib.suppress(OSError):
                leftover.unlink()


if __name__ == "__main__":
    sys.exit(main())
