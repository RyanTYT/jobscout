"""webapp/routers/profile.py — the master resume editor (form + upload)."""

from __future__ import annotations

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobscout.core import db
from jobscout.webapp.common import (
    TEMPLATES,
)
from jobscout.webapp.common import (
    ctx as page_ctx,
)


def register(app):
    @app.get("/profile/resume-download")
    def profile_resume_download():
        """Serve the current master resume as a download (fill it fully
        offline, re-upload via the form above)."""
        from starlette.responses import FileResponse

        from jobscout.core.paths import master_resume_dir

        path = master_resume_dir() / "resume.yaml"
        if not path.is_file():
            return RedirectResponse("/profile?upload_error=resume.yaml+not+found",
                                    status_code=303)
        return FileResponse(path, filename="resume.yaml",
                            media_type="application/x-yaml")

    @app.post("/profile/upload-resume")
    async def profile_upload_resume(request: Request):
        """Replace the master resume wholesale from an uploaded YAML file.
        Validated against the schema first; the old file is kept as .bak."""
        from urllib.parse import quote as _q

        from jobscout.webapp.stores import profile_store

        form = await request.form()
        upload = form.get("resume_file")
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse(
                "/profile?upload_error=" + _q("no file selected"),
                status_code=303)
        name = upload.filename.lower()
        if not name.endswith((".yaml", ".yml")):
            return RedirectResponse(
                "/profile?upload_error=" + _q("expected a .yaml file"),
                status_code=303)
        try:
            text = (await upload.read()).decode("utf-8")
        except UnicodeDecodeError:
            return RedirectResponse(
                "/profile?upload_error=" + _q("not a UTF-8 text file"),
                status_code=303)
        try:
            result = profile_store.upload_resume(text)
            return RedirectResponse(
                "/profile?uploaded=1&fields=" + _q(str(result["fields"])),
                status_code=303)
        except profile_store.ProfileError as e:
            return RedirectResponse(
                "/profile?upload_error=" + _q(str(e)), status_code=303)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = Query(""),
                     uploaded: str = Query(""), upload_error: str = Query(""),
                     fields: str = Query("")):
        from jobscout.webapp.stores import profile_store

        conn = db.connect()
        try:
            ctx = page_ctx("profile", conn)
        finally:
            conn.close()
        return TEMPLATES.TemplateResponse(
            request, "profile.html",
            {
                **ctx,
                "fields": profile_store.FIELDS,
                "field_options": {
                    f.key: [(o, o, None) for o in f.options]
                    for f in profile_store.FIELDS
                },
                "values": profile_store.current_values(),
                "missing": profile_store.missing_required(),
                "custom": profile_store.custom_fields(),
                "just_saved": saved == "1",
                "just_uploaded": uploaded == "1",
                "upload_error": upload_error,
                "uploaded_fields": fields,
                "error": None,
            },
        )

    @app.post("/profile/save")
    async def profile_save(request: Request):
        from jobscout.webapp.stores import profile_store

        form = dict(await request.form())
        try:
            profile_store.save_profile(form)
            return RedirectResponse("/profile?saved=1", status_code=303)
        except profile_store.ProfileError as e:
            conn = db.connect()
            try:
                ctx = page_ctx("profile", conn)
            finally:
                conn.close()
            return TEMPLATES.TemplateResponse(
                request, "profile.html",
                {
                    **ctx,
                    "fields": profile_store.FIELDS,
                    "field_options": {
                        f.key: [(o, o, None) for o in f.options]
                        for f in profile_store.FIELDS
                    },
                    "values": profile_store.current_values(),
                    "missing": profile_store.missing_required(),
                    "custom": profile_store.custom_fields(),
                    "just_saved": False,
                    "error": str(e),
                },
                status_code=422,
            )
