"""The pages a candidate sees, built as strings.

No template engine and no static files, on purpose. These two pages are the
only HTML this backend serves, and a template directory would mean a second
place for a deployment to be wrong about — a missing file here is a blank page
for somebody applying for a job.

Everything is inlined: the CSS, the script, and the form. Nothing is fetched
from anywhere, which is what lets ``app.hr.public`` send a Content-Security
Policy of ``default-src 'none'`` and mean it. It also means the page renders the
same whether or not the ERP's own frontend is up, and carries no link to it.

**Every value that comes from the database is escaped**, including the ones an
admin wrote rather than a candidate. A job description is typed by a person, and
"our own staff typed it" has never been a reason to trust a string.
"""

from __future__ import annotations

from html import escape
from typing import Any

from app.hr.schemas import PublicListingOut, PublicOpeningOut

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5c6370;
--line:#e2e5ea;--accent:#1f5eff;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#181b21;--ink:#e8eaee;
--muted:#9aa1ad;--line:#2a2e37;--accent:#6f96ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:44rem;margin:0 auto;padding:2rem 1.15rem 4rem}
header h1{font-size:1.75rem;line-height:1.2;margin:0 0 .4rem}
.meta{color:var(--muted);font-size:.92rem;margin:0 0 1.5rem}
.meta span+span:before{content:"•";margin:0 .5rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:1.4rem;margin:0 0 1.15rem}
.card h2{font-size:1.05rem;margin:0 0 .3rem}
.card .hint{color:var(--muted);font-size:.88rem;margin:0 0 1rem}
.prose{white-space:pre-wrap;margin:0 0 1rem}
label{display:block;font-weight:600;font-size:.92rem;margin:1rem 0 .3rem}
label:first-of-type{margin-top:0}
.req{color:var(--bad);margin-left:.15rem}
.help{color:var(--muted);font-size:.85rem;margin:.25rem 0 0;font-weight:400}
input,select,textarea{width:100%;padding:.6rem .7rem;font:inherit;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:8px}
input[type=checkbox]{width:auto;margin-right:.5rem}
textarea{min-height:7rem;resize:vertical}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
.check{display:flex;align-items:center;font-weight:400;margin-top:1rem}
button{width:100%;padding:.8rem;font:inherit;font-weight:600;color:#fff;
background:var(--accent);border:0;border-radius:8px;cursor:pointer;margin-top:1.5rem}
button[disabled]{opacity:.6;cursor:progress}
.note{padding:.9rem 1rem;border-radius:8px;margin:1rem 0 0;font-size:.93rem}
.note.bad{background:#fdecea;color:#7a1b13;border:1px solid #f3c4bf}
.note.good{background:#e7f4ec;color:#124d2e;border:1px solid #b8dcc6}
@media (prefers-color-scheme:dark){
.note.bad{background:#3a1714;color:#f6c8c2;border-color:#5d241e}
.note.good{background:#122a1c;color:#b6e2c6;border-color:#1f4a30}}
.closed{text-align:center;padding:2.5rem 1rem}
.job{display:block;text-decoration:none;color:inherit}
.job:hover{border-color:var(--accent)}
.job h2{margin:0 0 .25rem}
footer{color:var(--muted);font-size:.82rem;text-align:center;margin-top:2rem}
"""

_SCRIPT = """
(function(){
  var f=document.getElementById('f'),b=document.getElementById('b'),n=document.getElementById('n');
  if(!f) return;
  function say(kind,text){n.className='note '+kind;n.textContent=text;n.hidden=false;
    n.scrollIntoView({block:'nearest'});}
  f.addEventListener('submit',function(e){
    e.preventDefault();
    n.hidden=true; b.disabled=true; b.textContent='Sending…';
    var data=new FormData(f);
    // Unchecked boxes post nothing at all, which reads as "not answered"
    // rather than as "no". Say no explicitly.
    f.querySelectorAll('input[type=checkbox]').forEach(function(c){
      if(!c.checked) data.set(c.name,'false');
    });
    fetch(f.action,{method:'POST',body:data})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j};});})
      .then(function(r){
        if(r.ok){ f.hidden=true; say('good', r.body.message || 'Thank you.'); return; }
        say('bad', (r.body && r.body.detail) || 'Something went wrong. Please try again.');
      })
      .catch(function(){ say('bad','Could not reach the server. Please try again.'); })
      .finally(function(){ b.disabled=false; b.textContent='Submit application'; });
  });
})();
"""


def _page(title: str, body: str) -> str:
    """The shell. Identical for both pages, and deliberately complete on its own."""
    return (
        "<!doctype html><html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        # Candidates should find the role through the link they were sent, not
        # through a search engine that indexed a token.
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{escape(title)}</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">{body}</div></body></html>"
    )


def _meta_line(*parts: str | None) -> str:
    kept = [escape(p) for p in parts if p]
    return f'<p class="meta">{"".join(f"<span>{p}</span>" for p in kept)}</p>' if kept else ""


def _as_text(value: object) -> str:
    """A posting answer as a line of an advert.

    Booleans and lists reach here because a posting form may carry a checkbox
    or a multi-select, and ``str(True)`` on a job advert reads as a bug.
    """
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(v) for v in value)
    return str(value)


def _prose(heading: str, text: str | None) -> str:
    if not text:
        return ""
    return f'<h2>{escape(heading)}</h2><p class="prose">{escape(text)}</p>'


def _input(field: Any) -> str:
    """One question, as the control its type asks for."""
    name = escape(field.key)
    label = escape(field.label)
    required = " required" if field.required else ""
    star = '<span class="req" aria-hidden="true">*</span>' if field.required else ""
    help_text = f'<p class="help">{escape(field.help)}</p>' if field.help else ""
    default = field.default

    if field.type == "checkbox":
        checked = " checked" if default is True else ""
        return (
            f'<label class="check" for="{name}">'
            f'<input type="checkbox" id="{name}" name="{name}" value="true"{checked}>'
            f"{label}</label>{help_text}"
        )

    if field.type == "select":
        options = "".join(
            f'<option value="{escape(str(o))}"'
            f'{" selected" if str(o) == str(default) else ""}>{escape(str(o))}</option>'
            for o in (field.options or [])
        )
        control = (
            f'<select id="{name}" name="{name}"{required}>'
            f'<option value="">Choose…</option>{options}</select>'
        )
    elif field.type == "textarea":
        control = (
            f'<textarea id="{name}" name="{name}"{required}>'
            f"{escape(str(default)) if default is not None else ''}</textarea>"
        )
    elif field.type == "file":
        # accept is a convenience for the file picker, not a control: the server
        # checks the extension again in app.hr.documents.
        control = (
            f'<input type="file" id="{name}" name="{name}"{required} '
            'accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.webp,.txt,.rtf">'
        )
    else:
        html_type = {
            "number": "number", "percent": "number", "currency": "number",
            "date": "date",
        }.get(field.type, "text")
        value = f' value="{escape(str(default))}"' if default is not None else ""
        control = f'<input type="{html_type}" id="{name}" name="{name}"{value}{required}>'

    return f'<label for="{name}">{label}{star}</label>{control}{help_text}'


def application_page(view: PublicOpeningOut) -> str:
    """The whole application form for one opening."""
    head = (
        f"<header><h1>{escape(view.title)}</h1>"
        + _meta_line(view.department, view.location, view.employment_type, view.salary_range)
        + "</header>"
    )

    about = (
        _prose("About the role", view.summary)
        + _prose("What you would be doing", view.description)
        + _prose("What we are looking for", view.requirements)
        # Whatever else the posting form asked for, under its own labels. The
        # advert is the super admin's to shape; this renders it rather than
        # deciding what a job posting is allowed to say.
        + "".join(_prose(d.label, _as_text(d.value)) for d in view.details)
    )
    if view.closes_on:
        about += (
            f'<p class="meta"><span>Applications close '
            f"{escape(view.closes_on.isoformat())}</span></p>"
        )
    about_card = f'<section class="card">{about}</section>' if about else ""

    if not view.accepting:
        return _page(
            view.title,
            head
            + about_card
            + '<section class="card closed"><h2>Applications are closed</h2>'
            + f'<p class="hint">{escape(view.closed_message or "")}</p></section>',
        )

    # Grouped by section so a long form reads as several short ones.
    named = {s.key: s for s in view.sections}
    order: list[str | None] = []
    grouped: dict[str | None, list[Any]] = {}
    for field in view.fields:
        key = field.section if field.section in named else None
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(field)

    blocks = []
    for key in order:
        section = named.get(key or "")
        heading = f"<h2>{escape(section.name)}</h2>" if section else ""
        hint = f'<p class="hint">{escape(section.help)}</p>' if section and section.help else ""
        controls = "".join(_input(f) for f in grouped[key])
        blocks.append(f'<section class="card">{heading}{hint}{controls}</section>')

    form = (
        f'<form id="f" action="{escape(view.submit_url)}" method="post" '
        'enctype="multipart/form-data" novalidate>'
        + "".join(blocks)
        + '<button id="b" type="submit">Submit application</button>'
        + "</form>"
        + '<div id="n" hidden></div>'
    )
    footer = '<footer>Your details are used only to consider this application.</footer>'
    return _page(view.title, head + about_card + form + footer + f"<script>{_SCRIPT}</script>")


def careers_page(listings: list[PublicListingOut]) -> str:
    """The list of openings HR chose to advertise."""
    head = "<header><h1>Open roles</h1></header>"
    if not listings:
        return _page(
            "Open roles",
            head
            + '<section class="card closed"><h2>Nothing open right now</h2>'
            + '<p class="hint">Please check back another time.</p></section>',
        )

    cards = "".join(
        f'<a class="card job" href="{escape(job.apply_url)}">'
        f"<h2>{escape(job.title)}</h2>"
        + _meta_line(job.department, job.location, job.employment_type)
        + (f'<p class="prose">{escape(job.summary)}</p>' if job.summary else "")
        + (
            f'<p class="meta"><span>Closes {escape(job.closes_on.isoformat())}</span></p>'
            if job.closes_on
            else ""
        )
        + "</a>"
        for job in listings
    )
    return _page("Open roles", head + cards)
