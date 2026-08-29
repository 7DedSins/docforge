"""The public landing page.

Served from Python as one string rather than as static files behind a template
engine. It is a single page with no build step, no dependencies and no assets to
serve, which keeps the deployment to the four containers described in
ARCHITECTURE.md.

Placeholders {{HOST}}, {{FREE_TIER}} and {{DEMO_PER_DAY}} are substituted in
main.py so the page always matches the running configuration — a docs page that
disagrees with the service is worse than no docs page.
"""

LANDING_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DocForge — document conversion and image rendering API</title>
<meta name="description" content="Convert DOCX, XLSX and PPTX to PDF, render HTML to PDF, merge PDFs, and generate images from HTML templates. Self-hostable, with API keys and quotas built in.">
<style>
 :root { --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --accent:#1d4ed8; --bg:#f8fafc; }
 * { box-sizing:border-box; }
 body { margin:0; font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
        color:var(--ink); background:#fff; }
 .wrap { max-width:52rem; margin:0 auto; padding:0 1.5rem; }
 header { padding:4rem 0 2.5rem; }
 h1 { font-size:2.6rem; margin:0 0 .5rem; letter-spacing:-.02em; }
 .tag { color:var(--muted); font-size:1.15rem; margin:0 0 1.5rem; }
 .pills span { display:inline-block; background:var(--bg); border:1px solid var(--line);
        border-radius:99px; padding:.2rem .7rem; font-size:.82rem; color:var(--muted);
        margin:0 .3rem .4rem 0; }
 h2 { font-size:1.35rem; margin:2.5rem 0 .8rem; letter-spacing:-.01em; }
 a { color:var(--accent); }
 code { background:var(--bg); padding:.15rem .35rem; border-radius:4px; font-size:.88em; }
 pre { background:var(--ink); color:#e2e8f0; padding:1rem 1.1rem; border-radius:8px;
        overflow-x:auto; font-size:.85rem; line-height:1.5; }
 pre code { background:none; padding:0; color:inherit; }
 table { border-collapse:collapse; width:100%; font-size:.94rem; }
 th,td { text-align:left; padding:.55rem .7rem; border-bottom:1px solid var(--line); }
 th { font-weight:600; font-size:.82rem; text-transform:uppercase; letter-spacing:.04em;
        color:var(--muted); }
 /* demo */
 #drop { border:2px dashed var(--line); border-radius:12px; padding:2.5rem 1.5rem;
        text-align:center; background:var(--bg); transition:.15s; cursor:pointer; }
 #drop.over { border-color:var(--accent); background:#eff6ff; }
 #drop p { margin:.35rem 0; }
 .hint { color:var(--muted); font-size:.88rem; }
 #status { margin-top:.9rem; font-size:.94rem; min-height:1.4rem; }
 .err { color:#b91c1c; } .ok { color:#15803d; }
 .btn { display:inline-block; background:var(--accent); color:#fff; border:0;
        padding:.6rem 1.1rem; border-radius:7px; font-size:.95rem; cursor:pointer;
        text-decoration:none; }
 footer { margin:4rem 0 3rem; padding-top:1.5rem; border-top:1px solid var(--line);
        color:var(--muted); font-size:.88rem; }
 .cols { display:grid; grid-template-columns:1fr 1fr; gap:1.5rem; }
 @media (max-width:640px){ .cols{grid-template-columns:1fr} h1{font-size:2rem} }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>DocForge</h1>
  <p class="tag">Turn documents into PDFs, and HTML templates into images — over a simple API.</p>
  <div class="pills">
    <span>DOCX → PDF</span><span>HTML → PDF</span><span>PDF merge</span>
    <span>HTML → PNG</span><span>MCP server</span><span>Self-hostable</span>
  </div>
</header>

<h2>Try it — no signup</h2>
<div id="drop">
  <p><strong>Drop a Word, Excel or PowerPoint file here</strong></p>
  <p class="hint">or click to choose · converted to PDF in your browser's next breath</p>
  <p class="hint">{{DEMO_PER_DAY}} free conversions per day · files are never stored</p>
  <input id="file" type="file" hidden
         accept=".doc,.docx,.odt,.rtf,.txt,.xls,.xlsx,.ods,.csv,.ppt,.pptx,.odp">
</div>
<div id="status"></div>

<h2>Or call it from code</h2>
<pre><code>curl -X POST https://{{HOST}}/v1/convert/office \
  -H "Authorization: Bearer YOUR_KEY" \
  -F "file=@report.docx" -o report.pdf</code></pre>

<div class="cols">
<div>
<h2>Endpoints</h2>
<table>
<tr><th>Path</th><th>Does</th></tr>
<tr><td><code>/v1/convert/office</code></td><td>Office → PDF</td></tr>
<tr><td><code>/v1/convert/html</code></td><td>HTML → PDF</td></tr>
<tr><td><code>/v1/pdf/merge</code></td><td>Merge PDFs</td></tr>
<tr><td><code>/v1/image/render</code></td><td>Template → image</td></tr>
<tr><td><code>/v1/usage</code></td><td>Your usage</td></tr>
<tr><td><code>/mcp</code></td><td>MCP server</td></tr>
</table>
</div>
<div>
<h2>Plans</h2>
<table>
<tr><th>Plan</th><th>Per month</th></tr>
<tr><td>Free</td><td>{{FREE_TIER}} calls</td></tr>
<tr><td>Starter — $5</td><td>5,000</td></tr>
<tr><td>Pro — $25</td><td>50,000</td></tr>
</table>
<p class="hint">One unit per call, whatever the file size. Failed calls are
never billed.</p>
</div>
</div>

<h2>Why this exists</h2>
<p>Hosted conversion APIs bill per document — commonly around $0.08 each, which
is $1,600/month at 20,000 conversions. DocForge is <a
href="https://github.com/7DedSins/docforge">open source</a>: run it yourself for
the price of a small VPS, or let this instance run it for you.</p>
<p>Either way your documents are <strong>never written to disk</strong>. They
stream in, convert, and stream out.</p>

<h2>Interactive reference</h2>
<p><a class="btn" href="/docs">Open API docs</a></p>

<footer>
  <a href="https://github.com/7DedSins/docforge">Source on GitHub</a> ·
  MIT licensed · Built on
  <a href="https://gotenberg.dev">Gotenberg</a>
</footer>

</div>

<script>
(function () {
  var drop = document.getElementById('drop'),
      input = document.getElementById('file'),
      status = document.getElementById('status'),
      busy = false;

  function say(msg, cls) { status.className = cls || ''; status.textContent = msg; }

  drop.addEventListener('click', function () { if (!busy) input.click(); });
  input.addEventListener('change', function () { if (input.files[0]) send(input.files[0]); });

  ['dragenter', 'dragover'].forEach(function (e) {
    drop.addEventListener(e, function (ev) {
      ev.preventDefault(); drop.classList.add('over');
    });
  });
  ['dragleave', 'drop'].forEach(function (e) {
    drop.addEventListener(e, function (ev) {
      ev.preventDefault(); drop.classList.remove('over');
    });
  });
  drop.addEventListener('drop', function (ev) {
    if (!busy && ev.dataTransfer.files[0]) send(ev.dataTransfer.files[0]);
  });

  function send(file) {
    busy = true;
    say('Converting ' + file.name + '…');
    var fd = new FormData();
    fd.append('file', file);

    fetch('/try/convert', { method: 'POST', body: fd })
      .then(function (r) {
        if (!r.ok) {
          // Surface the server's own message — it explains quota and format
          // errors far better than anything generic we could write here.
          return r.json()
            .then(function (j) { throw new Error(j.detail || 'Conversion failed.'); },
                  function () { throw new Error('Conversion failed (' + r.status + ').'); });
        }
        var left = r.headers.get('X-Demo-Remaining');
        return r.blob().then(function (b) { return { blob: b, left: left }; });
      })
      .then(function (res) {
        var url = URL.createObjectURL(res.blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = file.name.replace(/\.[^.]+$/, '') + '.pdf';
        document.body.appendChild(a); a.click(); a.remove();
        // Revoking immediately can cancel the download in some browsers.
        setTimeout(function () { URL.revokeObjectURL(url); }, 30000);
        say('Done — check your downloads.' +
            (res.left !== null ? ' ' + res.left + ' left today.' : ''), 'ok');
      })
      .catch(function (e) { say(e.message, 'err'); })
      .finally(function () { busy = false; input.value = ''; });
  }
})();
</script>
</body>
</html>"""
