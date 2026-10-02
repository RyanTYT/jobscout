"""core/resume_pdf.py — HTML → PDF generation for tailored resumes.

Uses WeasyPrint to render clean resumes as PDFs. The HTML template
matches a professional one-page resume layout with CSS print styles.
"""

from __future__ import annotations

from pathlib import Path

_RESUME_CSS = """
@page {
  size: letter;
  margin: 0.6in 0.7in;
  @top-right {
    content: "";
  }
}
body {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: 10pt;
  line-height: 1.4;
  color: #1a1a1a;
}
h1 {
  font-size: 16pt;
  margin: 0 0 2pt 0;
  letter-spacing: 0.5pt;
}
h2 {
  font-size: 10.5pt;
  text-transform: uppercase;
  letter-spacing: 1pt;
  border-bottom: 1px solid #ccc;
  padding-bottom: 2pt;
  margin: 12pt 0 6pt 0;
}
h3 {
  font-size: 10pt;
  margin: 0;
  display: inline;
}
.contact {
  font-size: 9pt;
  color: #555;
  margin: 2pt 0 0 0;
}
.contact span { margin-right: 8pt; }
.experience-item, .project-item {
  margin-bottom: 8pt;
}
.experience-header, .project-header {
  display: flex;
  justify-content: space-between;
}
.experience-header .right, .project-header .right {
  font-size: 9pt;
  color: #555;
  text-align: right;
}
ul {
  margin: 2pt 0 0 0;
  padding-left: 16pt;
}
li {
  margin-bottom: 1pt;
  font-size: 9.5pt;
  line-height: 1.35;
}
.skills-row {
  font-size: 9.5pt;
  margin: 2pt 0;
}
.skills-row strong {
  display: inline-block;
  width: 90pt;
}
.summary {
  font-size: 9.5pt;
  margin: 6pt 0;
  line-height: 1.45;
}
"""


def _escape(text: str | None) -> str:
    if text is None:
        return ""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _build_html(tailored_md: str, *, style: str = _RESUME_CSS) -> str:
    """Convert tailored resume markdown to styled HTML for PDF output."""
    import markdown as _md

    body_html = _md.markdown(tailored_md, extensions=["extra"])
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>{style}</style>
</head><body>
{body_html}
</body></html>"""


def render_pdf(tailored_md: str, output_path: Path) -> Path:
    """Render tailored resume markdown to a PDF file."""
    from weasyprint import HTML

    html = _build_html(tailored_md)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(output_path))
    return output_path


def render_docx(tailored_md: str, output_path: Path) -> Path:
    """Render tailored resume markdown to a .docx file using pandoc if available,
    or fall back to a simple HTML → .docx conversion."""
    import shutil
    import subprocess
    import tempfile

    # Check if pandoc is available
    pandoc = shutil.which("pandoc")
    if pandoc:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as f:
            f.write(tailored_md)
            md_path = f.name
        try:
            subprocess.run(
                [pandoc, md_path, "-o", str(output_path)],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass  # fall through to HTML fallback
        finally:
            Path(md_path).unlink(missing_ok=True)
        if output_path.exists():
            return output_path

    # Fallback: save as .html (which Word can open)
    html = _build_html(tailored_md)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def resume_html(tailored_md: str) -> str:
    """Return the full HTML page for browser viewing (no PDF)."""
    return _build_html(tailored_md, style=_RESUME_CSS + """
body { margin: 0.6in 0.7in; }
""")