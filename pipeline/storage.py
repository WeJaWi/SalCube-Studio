"""Private Google Drive storage for completed video deliverables.

The implementation intentionally uses Drive's resumable-upload protocol
directly instead of an SDK.  Finished videos can be several GB, so every
request streams bounded chunks from disk and an interrupted upload records
its session URL in the job directory for a later resume.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode

import requests

from . import config


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class DriveError(RuntimeError):
    """A Drive request could not be completed safely."""


class DriveAuthorizationRequired(DriveError):
    """The OAuth client is configured but the account has not consented."""


class DriveUploadSessionExpired(DriveError):
    """Google no longer recognizes a persisted resumable-upload session."""


def _atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 -- matching Google Drive's checksum
    with path.open("rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _range_end(headers: dict) -> int:
    """Return the last confirmed byte from a resumable-upload response."""
    raw = (headers.get("Range") or "").strip()
    if "-" not in raw:
        return -1
    try:
        return int(raw.rsplit("-", 1)[1])
    except ValueError:
        return -1


class GoogleDriveStorage:
    """Small private Drive client used by the app's post-render hook."""

    def __init__(self, client_file: Optional[Path] = None,
                 token_file: Optional[Path] = None,
                 folder_name: Optional[str] = None,
                 chunk_bytes: Optional[int] = None):
        self.client_file = client_file or config.GDRIVE_CLIENT_SECRET_FILE
        self.token_file = token_file or config.GDRIVE_TOKEN_FILE
        self.folder_name = folder_name or config.GDRIVE_FOLDER_NAME
        self.chunk_bytes = chunk_bytes or config.GDRIVE_CHUNK_BYTES
        self._client: Optional[dict] = None
        self._token: Optional[dict] = None

    @property
    def configured(self) -> bool:
        return bool(self.client_file and self.client_file.is_file())

    @property
    def authorized(self) -> bool:
        return bool(self._load_token().get("refresh_token"))

    def status(self) -> dict:
        token = self._load_token()
        return {
            "configured": self.configured,
            "authorized": self.authorized,
            "folder_name": self.folder_name,
            "token_file": str(self.token_file),
            "account": token.get("account") or {},
        }

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        client = self._load_client()
        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            # Ensures a refresh token is returned even if this client was
            # previously authorized without one.
            "prompt": "consent",
            "state": state,
        }
        return f"{client['auth_uri']}?{urlencode(params)}"

    def complete_authorization(self, code: str, redirect_uri: str) -> None:
        client = self._load_client()
        r = requests.post(client["token_uri"], data={
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=60)
        self._require_ok(r, "OAuth token exchange")
        token = r.json()
        if not token.get("refresh_token"):
            raise DriveError("Google did not return a refresh token; revoke this app "
                             "in your Google Account and authorize again")
        if token.get("access_token"):
            token["expires_at"] = time.time() + int(token.get("expires_in") or 3600)
        # A newly selected account must never inherit the old account's cached
        # root/profile folder ids. Replace the token file instead of merging it.
        self._replace_token(token)
        try:
            about = self._request(
                "GET", f"{DRIVE_API}/about",
                params={"fields": "user(displayName,emailAddress)"}).json()
            account = about.get("user") or {}
            if account:
                self._save_token({"account": account})
        except Exception:  # noqa: BLE001 -- account label is non-essential
            pass

    def disconnect(self, *, revoke: bool = True) -> bool:
        """Forget the connected account and optionally revoke its OAuth grant.

        Local credentials are always cleared, even when Google cannot be
        reached. The boolean return tells the UI whether revocation succeeded.
        """
        token = dict(self._load_token())
        revoked = not bool(token.get("refresh_token") or token.get("access_token"))
        if revoke:
            credential = token.get("refresh_token") or token.get("access_token")
            if credential:
                try:
                    response = requests.post(
                        OAUTH_REVOKE_URL, params={"token": credential},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=30)
                    revoked = response.status_code == 200
                except requests.RequestException:
                    revoked = False
        self._token = {}
        self.token_file.unlink(missing_ok=True)
        return revoked

    def upload(self, source: Path, *, dest_name: Optional[str] = None,
               state_file: Optional[Path] = None,
               content_type: Optional[str] = None,
               folder_id: Optional[str] = None,
               file_id: Optional[str] = None) -> dict:
        """Upload and verify one local file, returning only small metadata.

        ``state_file`` lets a retry continue a valid Drive session after a
        process/network interruption.  The source is never deleted here; the
        caller is responsible for deletion after this function returns.
        """
        source = Path(source)
        if not source.is_file():
            raise DriveError(f"local deliverable is missing: {source.name}")
        total = source.stat().st_size
        if total <= 0:
            raise DriveError(f"refusing to upload empty file: {source.name}")
        dest_name = dest_name or source.name
        content_type = (content_type or mimetypes.guess_type(dest_name)[0]
                        or "application/octet-stream")
        state_file = state_file or source.with_name(f".{source.name}.gdrive-upload.json")
        state = self._load_upload_state(state_file, source, total)
        target_folder_id = folder_id or self.ensure_folder()
        if (state.get("folder_id") != target_folder_id
                or state.get("file_id") != file_id):
            state = {}
        folder_id = target_folder_id
        session_url = state.get("session_url")
        offset = -1
        if session_url:
            try:
                offset, completed = self._probe_session(session_url, total)
                if completed is not None:
                    verified = self._verify_upload(completed, source)
                    state_file.unlink(missing_ok=True)
                    return verified
            except DriveUploadSessionExpired:
                session_url = None  # expired sessions must be reopened
        if not session_url:
            session_url = self._start_session(dest_name, total, folder_id,
                                              content_type, file_id=file_id)
            offset = -1
            state = {"source_name": source.name, "size": total,
                     "folder_id": folder_id, "file_id": file_id,
                     "session_url": session_url}
            _atomic_json_write(state_file, state)

        result = self._send_chunks(source, total, session_url, state_file, state,
                                   offset + 1)
        verified = self._verify_upload(result, source)
        state_file.unlink(missing_ok=True)
        return verified

    def ensure_folder(self) -> str:
        """Create and remember the app-owned root folder named ``videos``."""
        token = self._load_token()
        existing = token.get("folder_id")
        if existing:
            r = self._request("GET", f"{DRIVE_API}/files/{existing}",
                              params={"fields": "id,mimeType,trashed"})
            if r.status_code == 200:
                data = r.json()
                if data.get("mimeType") == FOLDER_MIME and not data.get("trashed"):
                    return existing
            if r.status_code not in (404,):
                self._require_ok(r, "checking Drive folder")
        # Reconnecting the same Google account replaces the local token cache.
        # Reuse the app-created root instead of making another `videos` folder.
        found = self.find_file(self.folder_name, "root", mime_type=FOLDER_MIME)
        if found:
            folder_id = found["id"]
        else:
            r = self._request("POST", f"{DRIVE_API}/files", json={
                "name": self.folder_name,
                "mimeType": FOLDER_MIME,
            }, params={"fields": "id,name"})
            self._require_ok(r, "creating Drive videos folder")
            folder_id = r.json().get("id")
            if not folder_id:
                raise DriveError("Drive created the folder without returning its id")
        token["folder_id"] = folder_id
        token["folder_name"] = self.folder_name
        self._save_token(token)
        return folder_id

    def ensure_profile_folder(self, profile_name: str) -> str:
        """Return a stable child folder below the app's Drive root."""
        root_id = self.ensure_folder()
        token = self._load_token()
        folders = dict(token.get("profile_folders") or {})
        existing = folders.get(profile_name)
        if existing and self._folder_is_usable(existing, parent_id=root_id):
            return existing
        found = self.find_file(profile_name, root_id, mime_type=FOLDER_MIME)
        if found:
            folder_id = found["id"]
        else:
            r = self._request("POST", f"{DRIVE_API}/files", json={
                "name": profile_name, "mimeType": FOLDER_MIME,
                "parents": [root_id],
            }, params={"fields": "id,name"})
            self._require_ok(r, f"creating Drive folder for {profile_name}")
            folder_id = r.json().get("id")
            if not folder_id:
                raise DriveError(
                    f"Drive created the {profile_name} folder without returning its id")
        folders[profile_name] = folder_id
        self._save_token({"profile_folders": folders})
        return folder_id

    def find_file(self, name: str, folder_id: str,
                  *, mime_type: Optional[str] = None) -> Optional[dict]:
        """Find one exact-name, non-trashed child owned by this app."""
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        escaped_parent = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        query = (f"name = '{escaped_name}' and '{escaped_parent}' in parents "
                 "and trashed = false")
        if mime_type:
            escaped_mime = mime_type.replace("'", "\\'")
            query += f" and mimeType = '{escaped_mime}'"
        r = self._request("GET", f"{DRIVE_API}/files", params={
            "q": query, "pageSize": 10,
            "fields": "files(id,name,size,md5Checksum,mimeType,webViewLink,modifiedTime)",
            "orderBy": "modifiedTime desc",
        })
        self._require_ok(r, f"checking Drive for {name}")
        files = r.json().get("files") or []
        return files[0] if files else None

    def download_to_path(self, file_id: str, dest: Path) -> Path:
        """Download a private Drive file atomically to a local path."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        r = self.download_response(file_id)
        try:
            with tmp.open("wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
            if not tmp.is_file() or tmp.stat().st_size <= 0:
                raise DriveError("Drive downloaded an empty file")
            tmp.replace(dest)
            return dest
        finally:
            r.close()
            tmp.unlink(missing_ok=True)

    def download_response(self, file_id: str, byte_range: str = "") -> requests.Response:
        headers = {"Range": byte_range} if byte_range else {}
        r = self._request("GET", f"{DRIVE_API}/files/{file_id}",
                          params={"alt": "media"}, headers=headers, stream=True)
        self._require_ok(r, "downloading Drive video")
        return r

    def _load_client(self) -> dict:
        if self._client is not None:
            return self._client
        if not self.configured:
            raise DriveAuthorizationRequired(
                "Google Drive OAuth client JSON is not configured")
        try:
            raw = json.loads(self.client_file.read_text())
            client = raw.get("installed") or raw.get("web") or {}
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"could not read Google OAuth client JSON: {e}") from e
        required = ("client_id", "client_secret", "auth_uri", "token_uri")
        if not all(client.get(k) for k in required):
            raise DriveError("Google OAuth client JSON is missing required fields")
        self._client = client
        return client

    def _load_token(self) -> dict:
        if self._token is not None:
            return self._token
        try:
            self._token = json.loads(self.token_file.read_text())
        except FileNotFoundError:
            self._token = {}
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"could not read Drive token file: {e}") from e
        return self._token

    def _save_token(self, patch: dict) -> None:
        current = dict(self._load_token())
        current.update(patch)
        self._token = current
        _atomic_json_write(self.token_file, current)

    def _replace_token(self, token: dict) -> None:
        self._token = dict(token)
        _atomic_json_write(self.token_file, self._token)

    def _folder_is_usable(self, folder_id: str,
                          *, parent_id: Optional[str] = None) -> bool:
        r = self._request(
            "GET", f"{DRIVE_API}/files/{folder_id}",
            params={"fields": "id,mimeType,trashed,parents"})
        if r.status_code == 404:
            return False
        self._require_ok(r, "checking Drive profile folder")
        data = r.json()
        return bool(data.get("mimeType") == FOLDER_MIME
                    and not data.get("trashed")
                    and (not parent_id or parent_id in (data.get("parents") or [])))

    def _access_token(self, force_refresh: bool = False) -> str:
        token = self._load_token()
        if not token.get("refresh_token"):
            raise DriveAuthorizationRequired(
                "Google Drive needs one-time browser authorization")
        expires_at = float(token.get("expires_at") or 0)
        if (not force_refresh and token.get("access_token")
                and expires_at > time.time() + 120):
            return token["access_token"]
        client = self._load_client()
        r = requests.post(client["token_uri"], data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=60)
        self._require_ok(r, "refreshing Google Drive access")
        fresh = r.json()
        if not fresh.get("access_token"):
            raise DriveError("Google returned no access token while refreshing")
        fresh["expires_at"] = time.time() + int(fresh.get("expires_in") or 3600)
        self._save_token(fresh)
        return fresh["access_token"]

    def _request(self, method: str, url: str, *, headers: Optional[dict] = None,
                 retry_auth: bool = True, **kwargs) -> requests.Response:
        h = {"Authorization": f"Bearer {self._access_token()}"}
        h.update(headers or {})
        r = requests.request(method, url, headers=h, timeout=300, **kwargs)
        if r.status_code == 401 and retry_auth:
            h["Authorization"] = f"Bearer {self._access_token(force_refresh=True)}"
            r.close()
            r = requests.request(method, url, headers=h, timeout=300, **kwargs)
        return r

    def _start_session(self, dest_name: str, total: int, folder_id: str,
                       content_type: str,
                       file_id: Optional[str] = None) -> str:
        method = "PATCH" if file_id else "POST"
        url = (f"{DRIVE_UPLOAD_API}/files/{file_id}"
               if file_id else f"{DRIVE_UPLOAD_API}/files")
        metadata = {"name": dest_name}
        if not file_id:
            metadata["parents"] = [folder_id]
        r = self._request(method, url, params={
            "uploadType": "resumable", "supportsAllDrives": "true",
            "fields": "id,name,size,md5Checksum,webViewLink",
        }, headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(total),
        }, json=metadata)
        self._require_ok(r, "starting Drive resumable upload")
        session_url = r.headers.get("Location")
        if not session_url:
            raise DriveError("Drive did not return a resumable-upload session")
        return session_url

    def _probe_session(self, session_url: str, total: int) -> Tuple[int, Optional[dict]]:
        try:
            r = self._request("PUT", session_url, headers={
                "Content-Length": "0", "Content-Range": f"bytes */{total}",
            })
        except requests.RequestException as e:
            raise DriveError(f"could not resume Drive upload: {e}") from e
        if r.status_code in (200, 201):
            try:
                return total - 1, r.json()
            except ValueError as e:
                raise DriveError("completed Drive upload returned no metadata") from e
        if r.status_code == 308:
            return _range_end(r.headers), None
        if r.status_code in (404, 410):
            raise DriveUploadSessionExpired(
                f"Drive resumable session expired ({r.status_code})")
        self._require_ok(r, "checking Drive resumable upload")
        raise DriveError("Drive returned an invalid resumable-upload status")

    def _send_chunks(self, source: Path, total: int, session_url: str,
                     state_file: Path, state: dict, start: int) -> dict:
        pos = max(0, start)
        with source.open("rb") as src:
            src.seek(pos)
            while pos < total:
                block = src.read(min(self.chunk_bytes, total - pos))
                if not block:
                    raise DriveError("local file changed while uploading")
                end = pos + len(block) - 1
                final_response = None
                for attempt in range(5):
                    try:
                        r = self._request("PUT", session_url, headers={
                            "Content-Length": str(len(block)),
                            "Content-Range": f"bytes {pos}-{end}/{total}",
                        }, data=block)
                    except requests.RequestException:
                        r = None
                    if r is not None and r.status_code in (200, 201):
                        final_response = r
                        break
                    if r is not None and r.status_code == 308:
                        confirmed = _range_end(r.headers)
                        if confirmed >= end:
                            break
                    try:
                        confirmed, completed = self._probe_session(session_url, total)
                    except DriveError as e:
                        if attempt == 4:
                            raise e
                        time.sleep(min(30, 2 ** attempt))
                        continue
                    if completed is not None:
                        return completed
                    if confirmed >= end:
                        break
                    if attempt == 4:
                        detail = r.text[:200] if r is not None else "network failure"
                        raise DriveError(f"Drive upload failed at byte {pos}: {detail}")
                    time.sleep(min(30, 2 ** attempt))
                if final_response is not None:
                    try:
                        return final_response.json()
                    except ValueError as e:
                        raise DriveError("Drive completed upload without file metadata") from e
                confirmed, completed = self._probe_session(session_url, total)
                if completed is not None:
                    return completed
                pos = max(pos + len(block), confirmed + 1)
                src.seek(pos)
                state["confirmed_bytes"] = pos
                _atomic_json_write(state_file, state)
        raise DriveError("Drive upload ended without completion metadata")

    def _verify_upload(self, result: dict, source: Path) -> dict:
        file_id = result.get("id")
        remote_size = str(result.get("size") or "")
        expected_size = str(source.stat().st_size)
        if not file_id or remote_size != expected_size:
            raise DriveError("Drive upload verification failed: size/id mismatch")
        remote_md5 = result.get("md5Checksum")
        if remote_md5 and remote_md5 != _md5(source):
            raise DriveError("Drive upload verification failed: checksum mismatch")
        return {"id": file_id, "name": result.get("name") or source.name,
                "size": int(remote_size), "link": result.get("webViewLink") or "",
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    @staticmethod
    def _load_upload_state(path: Path, source: Path, total: int) -> dict:
        try:
            data = json.loads(path.read_text())
            if (data.get("source_name") == source.name
                    and int(data.get("size") or -1) == total):
                return data
        except Exception:  # noqa: BLE001
            pass
        return {}

    @staticmethod
    def _require_ok(r: requests.Response, action: str) -> None:
        if r.status_code < 400:
            return
        try:
            detail = r.json().get("error", {}).get("message") or r.text[:300]
        except Exception:  # noqa: BLE001
            detail = r.text[:300]
        raise DriveError(f"{action} failed ({r.status_code}): {detail}")
