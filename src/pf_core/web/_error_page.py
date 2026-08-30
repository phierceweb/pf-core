"""The error page: one self-contained HTML card, or JSON, by the request's Accept header."""

from __future__ import annotations

from html import escape as _html_escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

_ERROR_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f8fafc; color: #334155;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    text-align: center; max-width: 28rem;
  }}
  .code {{
    font-size: 5rem; font-weight: 700; color: #cbd5e1;
    line-height: 1; margin-bottom: 0.5rem; letter-spacing: -0.02em;
  }}
  h1 {{
    font-size: 1.25rem; font-weight: 600; color: #1e293b; margin-bottom: 0.5rem;
  }}
  p {{
    color: #64748b; margin-bottom: 2rem; line-height: 1.5;
  }}
  .actions {{
    display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap;
  }}
  a {{
    display: inline-block; padding: 0.5rem 1.25rem; border-radius: 0.375rem;
    font-size: 0.875rem; font-weight: 500; text-decoration: none;
    transition: background 0.15s, color 0.15s;
  }}
  .primary {{
    background: #2563eb; color: #fff;
  }}
  .primary:hover {{ background: #1d4ed8; }}
  .secondary {{
    background: #f1f5f9; color: #475569;
  }}
  .secondary:hover {{ background: #e2e8f0; }}
</style>
</head>
<body>
<div class="card">
  <div class="code">{code}</div>
  <h1>{heading}</h1>
  <p>{message}</p>
  <div class="actions">
    <a href="/" class="primary">Go home</a>
    <a href="javascript:history.back()" class="secondary">Go back</a>
  </div>
</div>
</body>
</html>"""

STATUS_HEADINGS = {
    400: "Bad request",
    403: "Forbidden",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Validation error",
    429: "Too many requests",
    500: "Something went wrong",
    502: "Bad gateway",
    503: "Service unavailable",
}

STATUS_MESSAGES = {
    400: "The request couldn't be processed. Check the input and try again.",
    403: "You don't have permission to access this.",
    404: "The page you're looking for doesn't exist or has been moved.",
    405: "This HTTP method isn't supported for this URL.",
    409: "The request conflicts with the current state of the resource.",
    422: "The submitted data didn't pass validation.",
    429: "You're sending too many requests. Please slow down.",
    500: "An unexpected error occurred. Try again or go back.",
    502: "The upstream server returned an invalid response.",
    503: "The service is temporarily unavailable. Try again in a moment.",
}


def render_error(
    request: Request,
    code: int,
    heading: str,
    message: str,
    *,
    app: FastAPI,
) -> HTMLResponse | JSONResponse:
    """Render an error as HTML or JSON based on Accept header."""
    accept = request.headers.get("accept", "")

    # If the project has a custom error template, use it
    if "text/html" in accept and hasattr(app.state, "templates"):
        try:
            return app.state.templates.TemplateResponse(
                request,
                "shared/error.html",
                {"title": heading, "code": code, "heading": heading, "message": message},
                status_code=code,
            )
        except Exception:
            pass  # Template missing or broken — fall through to built-in

    # Built-in self-contained error page for HTML requests. Escape all
    # interpolated text — message can carry request-reflected content.
    if "text/html" in accept:
        html = _ERROR_PAGE.format(
            title=_html_escape(f"{code} — {heading}"),
            code=code,
            heading=_html_escape(heading),
            message=_html_escape(message),
        )
        return HTMLResponse(html, status_code=code)

    # JSON for API clients
    return JSONResponse({"detail": message}, status_code=code)
